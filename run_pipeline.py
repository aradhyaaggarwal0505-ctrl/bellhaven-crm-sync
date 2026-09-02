#!/usr/bin/env python3
"""Entry point for cron / GitHub Actions: python run_pipeline.py [--dry-run]"""
from bellhaven_sync.pipeline import main

if __name__ == "__main__":
    main()
