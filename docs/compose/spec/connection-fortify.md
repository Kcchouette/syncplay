---
feature: connection-fortify
status: delivered
updated: 2026-07-14
source: fix/connection-fortify
---

# Fortifier la gestion des connexions Syncplay (ConnectionLost)

## Report

**What was built** — Coupures réseau sans plus dump le Twisted `Failure` dans l'UI (classification typée `ConnectionDone`/`ConnectionLost`/timeouts, 4 string-matches TLS conservés) ; `onDisconnect`/`pauseOnLeave` fiable sur perte inattendue uniquement (gates `_running` + `reconnecting`, pas sur quit ni `manualReconnect`) ; TCP keepalive (+intervalles Linux/macOS) + TCP_NODELAY côté client et serveur via `hardenTransport` ; backoff `backoffPolicy` 1→30 s avec jitter (remplace le plafond 3,2 s), notice 1× par streak, plus de retour `None` ; `destroyProtocol` detach-only ; serveur silencieux sur déconnexions propres/sales, 1 ligne sinon ; factory nettoyée ; 21 tests stdlib.

**Verification** — `python3 -m py_compile` (client/protocols/utils/constants/server) : PASS. `python3 -m unittest discover -s tests` : PASS 21/21 (5 runs stables). Smoke live local (serveur :8999 + client console) : Hello + State 1 Hz, 0 ligne `ERROR:`. Coupure (kill serveur en session) : 0 `ERROR:`, 1 debug + 1 notice reconnexion. Reconnexion (redémarrage serveur) : auto-Hello après backoff ~1/2/4 s. Sortie serveur sur déconnexion client propre : silencieuse. Revue indépendante : APPROVE_WITH_CHANGES → finding manualReconnect/pauseOnLeave corrigé (gate `reconnecting`) + test ajouté, re-vérifié PASS.

## [S1] Problem

En console, une coupure réseau produit deux lignes `ERROR:` brutales dumpant le Twisted `Failure` (`Connection to the other side was lost…`) plus la notice de reconnexion. Le reconnexion s'appuie sur un backoff plafonné à 3,2 s sans jitter, un `onDisconnect` inatteignable (`retries == 0` mort avec `ClientService`), pas de TCP keepalive, un double-drop depuis `connectionLost`, un serveur muet sur les déconnexions, et un potentiel `TypeError` si le retry policy retourne `None`.

## [S2] Design

### S2.1 — Silent `connectionLost` client + `onDisconnect` gate

`SyncClientProtocol.connectionLost` capture `wasLogged` / `wasRunning` avant traitement, classe le `reason` **par type** (`ConnectionDone` propre → debug ; `ConnectionLost`/`ConnectionAborted`/timeouts → debug 1 ligne sans dump UI ; 4 cas TLS string-match conservés ; inattendu → 1 ligne UI courte + stack debug), détache via `destroyProtocol()`, puis appelle `onDisconnect()` seulement si `wasLogged and wasRunning and not reconnecting` (perte inattendue ; `stop()` force `_running=False` avant drop → pas de pause à la quit ; `manualReconnect` pose `reconnecting=True` avant `stopService()` → pas de pause parasite).

### S2.2 — Hardening socket via `connectionMade`

Helper `hardenTransport(transport)` dans `utils.py` : `setTcpKeepAlive(True)`, `setTcpNoDelay(True)`, intervalles Linux `TCP_KEEPIDLE=30 / INTVL=10 / CNT=6` (macOS `TCP_KEEPALIVE=30`, Windows SO_KEEPALIVE seul), chaque option dans son `try/except` — ne lève jamais. Appliqué en 1re ligne de `SyncClientProtocol.connectionMade` et d'un nouveau `SyncServerProtocol.connectionMade`. Pas de `prepareConnection` (Twisted ≥18.7 vs floor 16.4, proxy sans méthodes transport).

### S2.3 — Backoff `backoffPolicy`

Constants `RECONNECT_INITIAL_DELAY=0.5`, `RECONNECT_MAX_DELAY=30.0` ; `backoffPolicy(initialDelay, maxDelay, factor=2.0)` → délais réels ≈1, 2, 4… 30 s + jitter 0-1 s (attempt 1-based). Notice reconnexion 1× par streak via `_performRetryStateReset(announce=(retries==1))`. Jamais de `None` retourné (branche RECONNECT_RETRIES → `RECONNECT_MAX_DELAY` + stop schedulé). `onDisconnect` sorti du retry.

### S2.4 — `destroyProtocol` detach-only

`destroyProtocol()` ne fait plus que `self._protocol = None` sans toucher au transport (hygiène detach/close). `stop()` appelle explicitement `drop()` avant. `connected()` clear `reconnecting`. `reconnecting` initialisé à `False` dans `__init__`. `SyncClientFactory` perd `retry`/`_timesTried` vestigiaux.

### S2.5 — Serveur `connectionLost` logging

`ConnectionDone`/`ConnectionLost` silencieux ; exception inattendue = 1 ligne print ; `removeWatcher` toujours appelé.

### S2.6 — Tests stdlib

`tests/` avec `unittest` (pas de framework externe, pas de reactor) : `hardenTransport` (fake transport, exceptions avalées), classification des raisons, portes `onDisconnect` (logged/running/manual reconnect), séquence de délais backoff (bornes jitter).

## [S3] Out of Scope

- Connexion initiale fail-fast (`whenConnected(failAfterFailures=1)`) conservée.
- Pas de heartbeat applicatif ; `PROTOCOL_TIMEOUT` et protocole JSON inchangés.
- Pas de bump `requirements.txt` ; pas de log observer Twisted global ; pas de refonte GUI.
- `manualReconnect` / `failedAttempts` non-reset : limite connue acceptée.
- Windows : intervalles keepalive non portables (SO_KEEPALIVE seul).

## Tasks

- [x] T1: Silent `connectionLost` + classification typée + `onDisconnect` gate — acceptance: coupure réseau sans ligne `ERROR:` dump Failure ; 1 debug + notice 1×/streak ; quit sans pause ; perte inattendue `logged` avec pause (covers: S2.1)
- [x] T2: `hardenTransport` + `connectionMade` client/serveur — acceptance: keepalive+NODELAY actifs sans casser TLS ; helper ne lève jamais (covers: S2.2)
- [x] T3: Backoff `backoffPolicy` + constantes + announce — acceptance: délais ≈1→30 s jitterés ; plus de 3,2 s ; aucun `None` (covers: S2.3)
- [x] T4: `destroyProtocol` detach + `stop()` drop + flags — acceptance: stop propre ; `reconnecting` clearé ; factory nettoyée (covers: S2.4)
- [x] T5: Logging serveur `connectionLost` — acceptance: type propre/sale silencieux, inattendu = 1 ligne, `removeWatcher` toujours (covers: S2.5)
- [x] T6: Tests unitaires stdlib — acceptance: `python3 -m unittest discover -s tests` vert (covers: S2.1-S2.3)

## Journey log

- Revues indépendantes (2 agents) : APPROVE_WITH_CHANGES + CORRECTIONS_NEEDED, intégrées — `prepareConnection` évité (floor Twisted 16.4 + proxy), `backoffPolicy` 1er délai = `initialDelay*factor`, `UserAbort` inexistant → `ConnectionAborted`, double-drop = no-op Twisted (hygiène).
- Revue post-implémentation (3e agent) : APPROVE_WITH_CHANGES, 0 CRITICAL — finding IMPORTANT `manualReconnect` → pause `pauseOnLeave` corrigé par gate `not reconnecting` + test `test_manual_reconnect_does_not_pause`. Flag `reconnecting` initialisé dans `__init__` (inatteignable via short-circuit mais un flag s'initialise).
- Test backoff `assertAlmostEqual(4*d1)` flaky à cause du jitter indépendant → bornes `[base, base+1)` par attempt (5 runs OK).
- Kill serveur → client voit `ConnectionDone` (FIN OS) et non `ConnectionLost` : classification correcte, notice de reconnexion toujours émise.
