"""Weekend signal-research runner: sentiment → forward-return studies written to
memory so the research agent (and humans) can see which social conditions
actually preceded moves — a learning channel that does not require fills.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .signal_research import is_promising, study_sentiment_signal


@dataclass
class SignalResearchReport:
    studies: list = field(default_factory=list)
    promising: list = field(default_factory=list)
    text: str = ""


def _bars_from_broker(broker, symbol: str, days: int = 90):
    """Adapt a broker bars DataFrame into objects with .date / .close."""
    try:
        df = broker.get_bars(symbol, days=days)
    except Exception:
        return []
    if df is None or len(df) == 0:
        return []
    out = []
    for idx, row in df.iterrows():
        ts = idx[1] if isinstance(idx, tuple) else idx
        out.append(type("B", (), {
            "date": str(ts)[:10],
            "close": float(row["close"]),
        })())
    return out


def run_signal_research(config, broker, *, horizon_days: int = 5) -> SignalResearchReport:
    """Study each universe symbol's stored sentiment vs subsequent returns."""
    from ..data.intel import IntelStore

    report = SignalResearchReport()
    intel_path = config.settings.paths.intel_db
    if not os.path.exists(intel_path):
        report.text = "# Signal research\n\nNo intel.db yet — nothing to study.\n"
        return report

    store = IntelStore(intel_path)
    try:
        lines = [
            "# Signal research — sentiment → forward return",
            "",
            f"Horizon: {horizon_days} trading days after a positive-polarity read.",
            "",
        ]
        for symbol in config.settings.universe.core:
            hist = store.sentiment_history(symbol, days=90)
            if not hist:
                continue
            # Normalize keys expected by study_sentiment_signal
            snaps = [{"symbol": symbol, "ts": h.get("ts") or h.get("timestamp"),
                      "polarity": h.get("polarity", 0)} for h in hist]
            bars = _bars_from_broker(broker, symbol, days=120)
            study = study_sentiment_signal(snaps, bars, horizon_days=horizon_days)
            if study is None:
                continue
            report.studies.append(study)
            flag = ""
            if is_promising(study):
                report.promising.append(study)
                flag = " **PROMISING candidate**"
            lines.append(f"- {study.summary()}{flag}")
        if not report.studies:
            lines.append("No studies produced (need sentiment history + bars).")
        else:
            lines.append("")
            lines.append(
                f"{len(report.promising)} / {len(report.studies)} studies clear "
                f"the promising gate (min 20 samples, hit rate ≥50%, positive avg)."
            )
        report.text = "\n".join(lines) + "\n"
        return report
    finally:
        store.close()


def persist_signal_research(config, report: SignalResearchReport) -> str:
    path = Path(config.settings.paths.memory_dir) / "signal_research.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.text, encoding="utf-8")
    return str(path)
