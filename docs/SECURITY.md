# Sicurezza

## Ambito

Portale, worker di sync, PostgreSQL e Grafana del progetto Control Room. Rete: solo LAN Control Room, dietro reverse
proxy con TLS.

## Dati

- I ticket GLPI dell'entità Control Room sono dati aziendali; dump, backup e audit SQLite vanno trattati come riservati.
- Nessun dato reale nel repository: GLPI simulato e dataset sintetico sono inventati.

## Autenticazione e autorizzazione

- **AD con bind dell'utente**: nessun account di servizio LDAP, quindi nessun segreto LDAP da custodire, e il portale
  non può leggere AD più di quanto possa l'utente che fa login (gestione-contratti usa invece `svc_contratti` perché
  il suo modulo cerca prima l'utente e poi verifica la password).
- Certificato del DC sempre verificato con la CA interna (`LDAP_CA_CERT_FILE`); `LDAP_VALIDATE_TLS=false` è vietato
  in produzione e blocca `deploy.sh`.
- Ruoli dai gruppi AD (vedi [SPEC §5](SPEC.md#5-accesso-e-ruoli)); senza gruppo, accesso negato.
- `DEV_AUTH` / `DEV_ROLES` disattivano l'AD: vietati in produzione, bloccano `deploy.sh`.
- Sessione di 12 h (`SESSION_HOURS`), cookie `Secure` e `SameSite=Strict`; rate limit del login in memoria.

## GLPI

- Account di servizio `svc-controlroom` con profilo limitato all'entità 14, non ricorsivo, senza diritti di
  amministrazione; client API limitato per IP all'host dell'app.
- L'operatore reale è tracciato nel testo di soluzione/ticket e nell'audit locale.

## Grafana

- Raggiungibile solo attraverso il portale e solo dal ruolo manager; non è pubblicato su porte dell'host.
- Auth proxy accettato solo dall'IP fisso dell'app (un altro container che invia `X-WEBAUTH-USER` riceve 401).
- Il proxy scarta sempre `X-WEBAUTH-*`, `X-Grafana-*` e `Authorization` inviati dal browser, non inoltra il cookie
  di sessione del portale, accetta solo GET/HEAD/POST e rifiuta l'username `GRAFANA_ADMIN_USER`.
- `allow_embedding` è attivo ma il proxy impone `frame-ancestors 'self'`.
- Login form, basic auth, accesso anonimo e Grafana Live sono disattivati: l'admin locale non può entrare;
  dashboard e datasource sono solo provisioning. Utenti creati come **Viewer**.

## Database

- Postgres non espone porte; `cr_sync` scrive lo schema `reporting`, `grafana_ro` ha solo SELECT in sessioni
  read-only (ogni `CREATE TABLE` fallisce, verificato).
- Grafana e Postgres sono su reti interne senza accesso a Internet.

## Segreti

- `config/.env` con permessi 600; segreti generati da `deploy.sh prepare`.
- Non committare `.env`, token, certificati o password.

## Checklist minima per una modifica

- La modifica cambia chi può leggere o scrivere su GLPI? Aggiornare SPEC e test dei ruoli.
- Tocca il proxy Grafana o gli header? Rieseguire `tests/test_grafana_proxy.py`.
- Introduce una variabile? Aggiornare `.env.example` e, se rilevante, i controlli di `deploy.sh`.
