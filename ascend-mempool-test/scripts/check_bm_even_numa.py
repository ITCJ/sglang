"""Compatibility entry point for the original even-NUMA report checker."""

from check_bm_numa import check_reports, main

__all__ = ["check_reports", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
