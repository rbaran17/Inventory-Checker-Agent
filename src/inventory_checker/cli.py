"""Command line: run the service, or seed a demo session."""

from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="inventory-checker")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="Run the ingestion and observation service")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8443)
    s.add_argument("--certfile", help="TLS certificate (phones only allow the camera over HTTPS)")
    s.add_argument("--keyfile")
    d = sub.add_parser("demo", help="Create a demo session with zones and a sample listing")
    d.add_argument("--lead", default="lead@firm.example")
    d.add_argument("--listing", default=str(Path(__file__).resolve().parents[2] / "samples" / "perpetual_listing.csv"))
    args = p.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("inventory_checker.api:app_from_env", factory=True, host=args.host, port=args.port,
                    ssl_certfile=args.certfile, ssl_keyfile=args.keyfile)
    elif args.cmd == "demo":
        from .config import Settings
        from .service import ObservationService
        from .store import Database, LocalWormEvidenceStore

        st = Settings()
        st.data_dir.mkdir(parents=True, exist_ok=True)
        svc = ObservationService(Database(st.db_path), LocalWormEvidenceStore(st.evidence_dir))
        sess = svc.create_session(args.lead, engagement="FY26 audit", client="Demo Manufacturing Ltd",
                                  location="Main warehouse", count_date="2026-12-31", tolerance_units=1,
                                  tolerance_pct=2, sample_size=3, sample_value_threshold=50000)
        for code, desc in [("A", "Raw materials"), ("B", "Finished goods"), ("Q", "Quarantine / damaged")]:
            svc.add_zone(args.lead, sess.id, code, desc)
        n = svc.import_listing(args.lead, sess.id, Path(args.listing).read_text())
        sample = svc.draw_sample(args.lead, sess.id, seed=7)
        print(f"Created session {sess.id}: {n} listing rows, {len(sample)} sampled. Sign in as {args.lead}.")


if __name__ == "__main__":
    main()
