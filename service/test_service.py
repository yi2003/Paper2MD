#!/usr/bin/env python3
"""Quick test: runs parse_exam.py on a sample image.

Usage:
    python test_service.py <image.jpg> [--output result.md] [--keep-temp]
"""

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python test_service.py <image.jpg> [--output result.md] [--keep-temp]")
        sys.exit(1)

    image = sys.argv[1]
    extra = sys.argv[2:] if len(sys.argv) > 2 else []

    cmd = ["python", "parse_exam.py", image] + extra
    print(f"Running: {' '.join(cmd)}\n")

    result = subprocess.run(cmd, capture_output=False)
    sys.exit(result.returncode)
