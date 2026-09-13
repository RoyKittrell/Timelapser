from pathlib import Path


def ensure_run_tree(run_dir: Path):
    run_dir = Path(run_dir)
    paths = {
        "run": run_dir,
        "frames": run_dir / "frames_jpeg",
        "ai_analysis": run_dir / "AI_Analysis",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths
