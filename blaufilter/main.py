from __future__ import annotations

import threading

import click

from vlcsync.vlc import VlcProcs

from blaufilter import config as bf_config
from blaufilter.controller import Controller
from blaufilter.finder import StaticCandidateFinder
from blaufilter.web import create_app


@click.command
@click.option("--hosts", "hosts", default=None, metavar="<host:port,host:port,...>",
              help="Override the candidate list (dev mode), same as BLAUFILTER_HOSTS.")
@click.option("--web-port", "web_port", type=int, default=None, help="Web UI port (default 8080).")
@click.option("--rate-nudge", "rate_nudge", is_flag=True, default=None,
              help="Enable smooth rate-based drift correction for small drifts.")
@click.option("--drift-threshold", "drift_threshold", type=float, default=None,
              help="Seek-correction threshold in seconds (default 0.5).")
@click.option("--config-path", "config_path", default=bf_config.CONFIG_PATH, show_default=True)
def main(hosts, web_port, rate_nudge, drift_threshold, config_path):
    """Blaufilter controller: discovers VLC instances on all devices, keeps them
    in sync and serves the web control UI."""
    cfg = bf_config.load(config_path)
    if hosts:
        cfg.dev_hosts = [h.strip() for h in hosts.split(",") if h.strip()]
    if web_port is not None:
        cfg.web_port = web_port
    if rate_nudge is not None:
        cfg.rate_nudge = rate_nudge
    if drift_threshold is not None:
        cfg.drift_threshold = drift_threshold

    print("Blaufilter controller starting...", flush=True)
    print(f"  Candidates: {['%s:%s' % c for c in cfg.candidate_addresses()]}", flush=True)

    env = VlcProcs({StaticCandidateFinder(cfg)})
    controller = Controller(cfg, env)

    app = create_app(controller)
    web_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=cfg.web_port, threaded=True),
        daemon=True,
    )
    web_thread.start()
    print(f"  Web UI on http://0.0.0.0:{cfg.web_port}", flush=True)

    try:
        controller.run()
    except KeyboardInterrupt:
        pass
    finally:
        controller.close()
        env.close()


if __name__ == "__main__":
    main()
