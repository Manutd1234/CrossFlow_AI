"""Compatibility entry point for the real-data local experiment runner.

Mutable routing costs may already be model outputs, not observed speed labels.
This entry point now uses source-specific real-data experiments. It never
silently creates synthetic data or connects to Supabase.
"""
if __package__:
    from .train_ml_engine import main
else:
    from train_ml_engine import main

if __name__=='__main__':
    raise SystemExit(main())
