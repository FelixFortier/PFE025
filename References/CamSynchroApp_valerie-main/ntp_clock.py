"""
ntp_clock.py – Horloge NTP fournissant un temps absolu (UTC) indépendant
des horloges internes des caméras.

Principe :
  1. Interroge périodiquement un serveur NTP pour obtenir l'offset entre
     l'horloge locale (monotonic) et le temps UTC réel.
  2. Expose une méthode `now()` renvoyant un timestamp UTC corrigé
     (précision sous la milliseconde en réseau local).
"""

import threading
import time
import ntplib


class NTPClock:
    """Horloge synchronisée à un serveur NTP."""

    def __init__(self, server: str = "pool.ntp.org", sync_interval: float = 30.0):
        self._server = server
        self._sync_interval = sync_interval
        self._ntp_offset: float = 0.0  # offset NTP (sec)
        self._last_sync_time: float = 0.0  # monotonic time of last successful sync
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        # Première synchronisation bloquante
        self._sync_once()

    # ── API publique ────────────────────────────────────────────────────────

    def now(self) -> float:
        """Renvoie le timestamp UTC courant corrigé par l'offset NTP."""
        with self._lock:
            offset = self._ntp_offset
        return time.time() + offset

    def start(self):
        """Démarre la synchronisation périodique en arrière-plan."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Arrête la synchronisation périodique."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def offset(self) -> float:
        """Offset NTP courant (secondes)."""
        with self._lock:
            return self._ntp_offset

    @property
    def last_sync_time(self) -> float:
        """Monotonic timestamp of last successful NTP sync (0 if never)."""
        with self._lock:
            return self._last_sync_time

    # ── Internals ───────────────────────────────────────────────────────────

    def _sync_once(self):
        """Interroge le serveur NTP et met à jour l'offset."""
        try:
            client = ntplib.NTPClient()
            response = client.request(self._server, version=3)
            with self._lock:
                old_offset = self._ntp_offset
                self._ntp_offset = response.offset
                self._last_sync_time = time.monotonic()
            # Trace les sauts d'offset significatifs (> 1 s) qui peuvent
            # introduire des discontinuités dans les timestamps caméras.
            if abs(response.offset - old_offset) > 1.0 and old_offset != 0.0:
                print(
                    f"[NTP] Saut d'offset : {old_offset:+.3f}s "
                    f"\u2192 {response.offset:+.3f}s", flush=True,
                )
        except Exception as exc:
            # En cas d'échec, on garde l'offset précédent.
            # On loggue uniquement si la désynchronisation devient critique
            # (> 2 min depuis le dernier succès) pour éviter le spam.
            with self._lock:
                stale_for = (
                    time.monotonic() - self._last_sync_time
                    if self._last_sync_time > 0 else 0.0
                )
            if stale_for > 120.0:
                print(
                    f"[NTP] Sync \u00e9chou\u00e9e ({exc}) \u2014 offset stale "
                    f"depuis {stale_for:.0f}s, valeur courante "
                    f"{self._ntp_offset:+.3f}s",
                    flush=True,
                )

    def _sync_loop(self):
        """Boucle d'arrière-plan pour la synchronisation périodique."""
        while self._running:
            self._sync_once()
            # Attente interruptible
            end = time.monotonic() + self._sync_interval
            while self._running and time.monotonic() < end:
                time.sleep(0.5)
