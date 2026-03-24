#!/usr/bin/env python3
"""
organize_transcripts.py — Extract, categorize, rename, and clean Voice Memo transcripts.

Phase 1 (extract): Scans macOS Voice Memos for new recordings and saves raw transcripts
  to transcripts/YYYY-MM-DD_HHMMSS.txt. Already-committed files are skipped.

Phase 2 (organize): Categorizes and cleans only unprocessed transcripts using Claude API,
  writing to college/, work/, or other/. Tracks processed files in transcripts/.processed_index
  so nothing is reprocessed on repeat runs.

Usage:
  export ANTHROPIC_API_KEY=sk-...
  python3 organize_transcripts.py              # full run (extract + organize)
  python3 organize_transcripts.py --dry-run    # preview only (no writes/commits)
  python3 organize_transcripts.py --limit 10   # organize only first N unprocessed files
  python3 organize_transcripts.py --skip-extract   # skip Phase 1 (organize only)
  python3 organize_transcripts.py --skip-organize  # skip Phase 2 (extract only)

Requirements: Python 3.6+, git, ANTHROPIC_API_KEY.
Run from Terminal.app with Full Disk Access enabled (for Voice Memos access).
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import anthropic
except ImportError:
    sys.exit("ERROR: anthropic SDK not installed. Run: pip3 install anthropic")

REPO_ROOT = Path(__file__).resolve().parent
TRANSCRIPTS_DIR = REPO_ROOT / "transcripts"
PROCESSED_INDEX = TRANSCRIPTS_DIR / ".processed_index"
CATEGORIES = ["college", "work", "other"]

RECORDINGS_DIR = (
    Path.home() / "Library" / "Group Containers" /
    "group.com.apple.VoiceMemos.shared" / "Recordings"
)

SYSTEM_PROMPT = """\
You are processing Voice Memo transcripts recorded by an Indian professional/student.
Audio was auto-transcribed and contains Indian English speech patterns and transcription errors
(misheard words, missing punctuation, run-on sentences, filler sounds like ". " at sentence starts).

Return ONLY valid JSON — no markdown, no explanation, just the JSON object.\
"""

USER_PROMPT_TEMPLATE = """\
Transcript (first 3000 chars):
<transcript>
{content}
</transcript>

Return JSON with exactly these keys:
{{
  "category": "college" | "work" | "other",
  "title": "3-6-word-hyphenated-slug",
  "cleaned": "full corrected transcript text"
}}

Rules:
- category: college = lectures/classes/academic; work = meetings/standups/tickets/tech discussions; other = personal/test/misc
- title: lowercase hyphenated slug, 3-6 words, descriptive of the main topic
- cleaned: fix grammar, spelling, punctuation; remove filler ". " artifacts; improve sentence flow;
  preserve all meaning and speaker voice; if multiple speakers, preserve the dialogue structure\
"""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def git_run(*args, fatal=True):
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        msg = f"git {' '.join(args)} failed: {result.stderr.strip()}"
        if fatal:
            sys.exit(f"ERROR: {msg}")
        else:
            print(f"  WARNING: {msg}")
    return result.stdout.strip()


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s]+', '-', text)
    text = re.sub(r'-+', '-', text).strip('-')
    return text[:60]


def date_prefix(filename: str) -> str:
    """Extract YYYY-MM-DD from a timestamp filename like 2025-08-16_165755.txt"""
    return filename[:10]


# ---------------------------------------------------------------------------
# Phase 1: Extract new Voice Memos → transcripts/
# ---------------------------------------------------------------------------

def find_all_m4a() -> list:
    """Return all .m4a files sorted oldest-first by modification time."""
    m4a_files = list(RECORDINGS_DIR.rglob("*.m4a"))
    return sorted(m4a_files, key=lambda p: p.stat().st_mtime)


def find_sidecar_json(m4a_path: Path):
    json_files = list(m4a_path.parent.glob("*.json"))
    if not json_files:
        return None
    return max(json_files, key=lambda p: p.stat().st_mtime)


def extract_from_sidecar(json_path: Path) -> str:
    with open(json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    try:
        chunks = data["SpeechRecognitionResult"]["STChunks"]
    except (KeyError, TypeError):
        return ""
    parts = [chunk.get("STString", "").strip() for chunk in chunks if chunk.get("STString")]
    return " ".join(parts)


def extract_from_m4a(m4a_path: Path) -> str:
    """Extract transcript from the tsrp atom embedded in the .m4a binary."""
    data = m4a_path.read_bytes()
    idx = data.find(b'tsrp')
    if idx == -1:
        return ""
    json_bytes = data[idx + 4:]
    brace_start = json_bytes.find(b'{')
    if brace_start == -1:
        return ""
    depth = 0
    for i, b in enumerate(json_bytes[brace_start:], brace_start):
        if b == ord('{'):
            depth += 1
        elif b == ord('}'):
            depth -= 1
            if depth == 0:
                raw = json_bytes[brace_start:i + 1].decode('utf-8', errors='replace')
                try:
                    obj = json.loads(raw)
                    attr = obj["attributedString"]
                    if isinstance(attr, list):
                        runs = attr
                    elif isinstance(attr, dict):
                        runs = attr.get("runs", [])
                    else:
                        return ""
                    words = [r for r in runs if isinstance(r, str)]
                    return " ".join(w.strip() for w in words if w.strip())
                except (json.JSONDecodeError, KeyError, TypeError):
                    return ""
    return ""


def get_transcript(m4a_path: Path) -> str:
    json_path = find_sidecar_json(m4a_path)
    if json_path:
        transcript = extract_from_sidecar(json_path)
        if transcript:
            return transcript
    return extract_from_m4a(m4a_path)


def recording_timestamp(m4a_path: Path) -> str:
    dt = datetime.fromtimestamp(m4a_path.stat().st_mtime)
    return dt.strftime("%Y-%m-%d_%H%M%S")


def is_committed(relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", relative_path],
        cwd=REPO_ROOT, capture_output=True,
    )
    return result.returncode == 0


def phase1_extract(dry_run: bool) -> list:
    """Extract new Voice Memos to transcripts/. Returns list of new filenames written."""
    print("=== Phase 1: Extract new Voice Memos ===\n")

    if not RECORDINGS_DIR.is_dir():
        print(f"  SKIP: Recordings directory not found: {RECORDINGS_DIR}")
        print("  (This script must run on macOS with Full Disk Access enabled.)\n")
        return []

    all_m4a = find_all_m4a()
    if not all_m4a:
        print("  No .m4a recordings found.\n")
        return []

    print(f"  Found {len(all_m4a)} recording(s). Checking for new ones...\n")
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)

    new_files = []
    skipped_committed = 0
    skipped_no_transcript = 0

    for m4a_path in all_m4a:
        timestamp = recording_timestamp(m4a_path)
        out_filename = f"{timestamp}.txt"
        out_path = TRANSCRIPTS_DIR / out_filename
        relative_out = str(out_path.relative_to(REPO_ROOT))

        if out_path.exists() and is_committed(relative_out):
            skipped_committed += 1
            continue

        transcript = get_transcript(m4a_path)
        if not transcript:
            skipped_no_transcript += 1
            print(f"  [skip] {m4a_path.name} — no transcript found")
            continue

        if dry_run:
            print(f"  [dry-run] Would write: {relative_out}  ({transcript[:60]}...)")
            new_files.append(out_filename)
        else:
            out_path.write_text(transcript, encoding="utf-8")
            new_files.append(out_filename)
            print(f"  [write] {relative_out} ({len(transcript)} chars)")

    print(f"\n  Already committed: {skipped_committed}")
    print(f"  No transcript    : {skipped_no_transcript}")
    print(f"  New transcripts  : {len(new_files)}")

    if not dry_run and new_files:
        print(f"\n  Committing {len(new_files)} new transcript(s)...")
        for fn in new_files:
            git_run("add", f"transcripts/{fn}")
        git_run("commit", "-m", f"Add {len(new_files)} transcript(s) from Voice Memos")
        git_run("push", "origin", "main")
        print("  Committed and pushed.")

    print()
    return new_files


# ---------------------------------------------------------------------------
# Phase 2: Organize unprocessed transcripts → college/ work/ other/
# ---------------------------------------------------------------------------

def load_processed_index() -> set:
    """Return set of already-processed transcript filenames from .processed_index."""
    if PROCESSED_INDEX.exists():
        lines = PROCESSED_INDEX.read_text(encoding="utf-8").splitlines()
        return set(line.strip() for line in lines if line.strip())
    return set()


def init_processed_index_from_git() -> set:
    """
    Bootstrap: return all transcript filenames currently committed in transcripts/.
    Called when .processed_index doesn't exist yet, to avoid re-organizing historical files.
    """
    output = git_run("ls-files", "transcripts/", fatal=False)
    committed = set()
    for line in output.splitlines():
        fname = Path(line).name
        if fname.endswith(".txt"):
            committed.add(fname)
    return committed


def save_processed_index(processed: set, dry_run: bool):
    if dry_run:
        return
    lines = sorted(processed)
    PROCESSED_INDEX.write_text("\n".join(lines) + "\n", encoding="utf-8")


def process_transcript(client: anthropic.Anthropic, content: str) -> dict:
    """Call Claude API and return {category, title, cleaned}."""
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": USER_PROMPT_TEMPLATE.format(content=content[:3000])
        }]
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    return json.loads(raw)


def phase2_organize(dry_run: bool, limit: int, newly_extracted: list, client: anthropic.Anthropic):
    """Organize unprocessed transcripts using Claude API."""
    print("=== Phase 2: Organize new transcripts ===\n")

    # Load (or bootstrap) the processed index
    bootstrapped = False
    if PROCESSED_INDEX.exists():
        processed = load_processed_index()
    else:
        print("  No .processed_index found — bootstrapping from git history...")
        processed = init_processed_index_from_git()
        # Exclude newly extracted files so they get processed this run
        processed -= set(newly_extracted)
        bootstrapped = True
        print(f"  Bootstrapped with {len(processed)} already-committed transcript(s).\n")

    # Find unprocessed transcripts
    all_transcripts = sorted(TRANSCRIPTS_DIR.glob("*.txt"))
    to_process = [p for p in all_transcripts if p.name not in processed]

    if not to_process:
        print("  Nothing new to organize.\n")
        # Still write the index if we just bootstrapped it
        if bootstrapped and not dry_run:
            save_processed_index(processed, dry_run)
            git_run("add", str(PROCESSED_INDEX.relative_to(REPO_ROOT)))
            git_run("commit", "-m", "Initialize .processed_index for transcript tracking", fatal=False)
            git_run("push", "origin", "main", fatal=False)
            print("  Bootstrapped .processed_index committed.\n")
        return

    if limit:
        to_process = to_process[:limit]

    total = len(to_process)
    print(f"  Organizing {total} transcript(s){'  [DRY RUN]' if dry_run else ''}...\n")

    if not dry_run:
        for cat in CATEGORIES:
            (REPO_ROOT / cat).mkdir(exist_ok=True)

    written = []
    errors = []
    counts = {cat: 0 for cat in CATEGORIES}

    for i, src_path in enumerate(to_process, 1):
        content = src_path.read_text(encoding="utf-8").strip()
        date = date_prefix(src_path.name)

        try:
            result = process_transcript(client, content)
            category = result.get("category", "other").lower()
            if category not in CATEGORIES:
                category = "other"
            title_slug = slugify(result.get("title", src_path.stem))
            cleaned = result.get("cleaned", content).strip()
        except Exception as e:
            print(f"  [{i}/{total}] ERROR on {src_path.name}: {e}")
            category = "other"
            title_slug = src_path.stem
            cleaned = content
            errors.append(src_path.name)

        out_filename = f"{date}_{title_slug}.txt"
        out_path = REPO_ROOT / category / out_filename
        rel_path = f"{category}/{out_filename}"

        print(f"  [{i}/{total}] {src_path.name} → {rel_path}")
        if not dry_run:
            out_path.write_text(cleaned, encoding="utf-8")
            written.append(rel_path)
            processed.add(src_path.name)

        counts[category] += 1
        time.sleep(0.3)

    print(f"\n  --- Summary ---")
    for cat in CATEGORIES:
        print(f"  {cat:8}: {counts[cat]}")
    if errors:
        print(f"  errors  : {len(errors)} (saved as-is in other/)")

    if dry_run or not written:
        if dry_run:
            print("\n  Dry run complete — nothing written.")
        else:
            print("\n  Nothing to commit.")
        return

    # Write updated index and commit everything
    save_processed_index(processed, dry_run)
    print(f"\n  Committing {len(written)} organized file(s) + updated index...")
    for cat in CATEGORIES:
        cat_path = REPO_ROOT / cat
        if cat_path.is_dir() and any(cat_path.iterdir()):
            git_run("add", cat, fatal=False)
    git_run("add", str(PROCESSED_INDEX.relative_to(REPO_ROOT)))
    git_run("commit", "-m",
            f"Organize {len(written)} transcript(s) — categorize, title, clean\n\n"
            f"college: {counts['college']}  work: {counts['work']}  other: {counts['other']}")
    git_run("push", "origin", "main")
    print("  Done — committed and pushed.\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing files or committing.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Organize only the first N unprocessed transcripts (0 = all).")
    parser.add_argument("--skip-extract", action="store_true",
                        help="Skip Phase 1 (extraction). Only organize.")
    parser.add_argument("--skip-organize", action="store_true",
                        help="Skip Phase 2 (organization). Only extract.")
    args = parser.parse_args()

    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and not args.skip_organize:
        sys.exit("ERROR: ANTHROPIC_API_KEY environment variable not set.")

    client = anthropic.Anthropic(api_key=api_key) if api_key else None

    newly_extracted = []
    if not args.skip_extract:
        newly_extracted = phase1_extract(args.dry_run)

    if not args.skip_organize:
        phase2_organize(args.dry_run, args.limit, newly_extracted, client)


if __name__ == "__main__":
    main()
