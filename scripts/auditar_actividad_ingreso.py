"""Compatibility entry point for Microsoft activity enrichment."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.audit_reports import *  # noqa: F403

if __name__ == "__main__":
    main()
