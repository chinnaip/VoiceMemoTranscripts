#!/usr/bin/env python3
"""
save_all_transcripts.py — Extract transcripts from ALL macOS Voice Memos and commit them.

Iterates every .m4a file in the Voice Memos recordings directory, extracts the
transcript (from a JSON sidecar if present, otherwise from the embedded tsrp atom),
and writes each to transcripts/YYYY-MM-DD_HHMMSS.txt. Already-committed files are
skipped (idempotent). All new files are committed in a single batch commit.

Usage:
    python3 save_all_transcripts.py            # process all recordings
    python3 save_all_transcripts.py --dry-run  # preview without writing/committing

Requirements: Python 3.6+, git (configured with push access to this repo).
Run from Terminal.app with Full Disk Access enabled.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

RECORDINGS_DIR = Path.home() / "Library" / "Group Containers" / \
    "group.com.apple.VoiceMemos.shared" / "Recordings"

REPO_ROOT = Path(__file__).resolve().parent
TRANSCRIPTS_DIR = REPO_ROOT / "transcripts"


def find_all_m4a(recordings_dir: Path) -> list:
    """Return all .m4a files sorted oldest-first by modification time."""
    m4a_files = list(recordings_dir.rglob("*.m4a"))
    if not m4a_files:
        sys.exit(
            f"ERROR: No .m4a files found under:\n  {recordings_dir}\n"
            "Make sure you have at least one Voice Memo recording on this Mac.\n"
            "Run from Terminal.app with Full Disk Access enabled."
        )
    return sorted(m4a_files, key=lambda p: p.stat().st_mtime)


def find_sidecar_json(m4a_path: Path):
    """Return the .json sidecar alongside m4a_path, or None."""
    json_files = list(m4a_path.parent.glob("*.json"))
    if not json_files:
        return None
    return max(json_files, key=lambda p: p.stat().st_mtime)


def extract_from_sidecar(json_path: Path) -> str:
    """Extract transcript from a JSON sidecar (SpeechRecognitionResult/STChunks)."""
    with open(json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    try:
        chunks = data["SpeechRecognitionResult"]["STChunks"]
    except (KeyError, TypeError):
        return ""
    parts = [chunk.get("STString", "").strip() for chunk in chunks if chunk.get("STString")]
    return " ".join(parts)


def extract_from_m4a(m4a_path: Path) -> str:
    """Extract transcript from the tsrp atom embedded in the .m4a binary.

    Structure: tsrp{"attributedString": ["word", {"timeRange": [...]}, ...], "locale": {...}}
    """
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
                        # Sequoia format: ["word", {"timeRange": [...]}, ...]
                        runs = attr
                    elif isinstance(attr, dict):
                        # Older format: {"runs": ["word", <idx>, ...], "attributeTable": [...]}
                        runs = attr.get("runs", [])
                    else:
                        return ""
                    words = [r for r in runs if isinstance(r, str)]
                    return " ".join(w.strip() for w in words if w.strip())
                except (json.JSONDecodeError, KeyError, TypeError):
                    return ""
    return ""


def get_transcript(m4a_path: Path) -> str:
    """Try sidecar JSON first, then tsrp atom. Returns empty string if none found."""
    json_path = find_sidecar_json(m4a_path)
    if json_path:
        transcript = extract_from_sidecar(json_path)
        if transcript:
            return transcript
    return extract_from_m4a(m4a_path)


def recording_timestamp(m4a_path: Path) -> str:
    """Return 'YYYY-MM-DD_HHMMSS' from the .m4a modification time."""
    dt = datetime.fromtimestamp(m4a_path.stat().st_mtime)
    return dt.strftime("%Y-%m-%d_%H%M%S")


def is_committed(relative_path: str) -> bool:
    """Return True if the file is already tracked by git."""
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", relative_path],
        cwd=REPO_ROOT, capture_output=True,
    )
    return result.returncode == 0


def git_run(*args):
    cmd = ["git", *args]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"ERROR: git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing or committing anything.")
    args = parser.parse_args()

    if not RECORDINGS_DIR.is_dir():
        sys.exit(
            f"ERROR: Recordings directory not found:\n  {RECORDINGS_DIR}\n"
            "This script must be run on macOS with the Voice Memos app installed.\n"
            "Run from Terminal.app with Full Disk Access enabled."
        )

    TRANSCRIPTS_DIR.mkdir(exist_ok=True)

    all_m4a = find_all_m4a(RECORDINGS_DIR)
    print(f"Found {len(all_m4a)} recording(s). Processing...\n")

    new_files = []
    skipped_no_transcript = 0
    skipped_already_committed = 0

    for m4a_path in all_m4a:
        timestamp = recording_timestamp(m4a_path)
        out_filename = f"{timestamp}.txt"
        out_path = TRANSCRIPTS_DIR / out_filename
        relative_out = str(out_path.relative_to(REPO_ROOT))

        # Skip if already committed
        if out_path.exists() and is_committed(relative_out):
            skipped_already_committed += 1
            continue

        transcript = get_transcript(m4a_path)
        if not transcript:
            skipped_no_transcript += 1
            print(f"  [skip] {m4a_path.name} — no transcript found")
            continue

        if args.dry_run:
            print(f"  [dry-run] Would write: {relative_out}")
            print(f"            Preview: {transcript[:80]}...")
        else:
            out_path.write_text(transcript, encoding="utf-8")
            new_files.append(relative_out)
            print(f"  [write] {relative_out} ({len(transcript)} chars)")

    print(f"\n--- Summary ---")
    print(f"  Already committed : {skipped_already_committed}")
    print(f"  No transcript     : {skipped_no_transcript}")

    if args.dry_run:
        print(f"  Would write       : {len(all_m4a) - skipped_already_committed - skipped_no_transcript} file(s)")
        return

    print(f"  New transcripts   : {len(new_files)}")

    if not new_files:
        print("\nNothing new to commit.")
        return

    # Batch commit all new transcripts
    for f in new_files:
        git_run("add", f)
    git_run("commit", "-m", f"Add {len(new_files)} transcript(s) from Voice Memos")
    git_run("push", "origin", "main")
    print(f"\nCommitted and pushed {len(new_files)} transcript(s).")


if __name__ == "__main__":
    main()
