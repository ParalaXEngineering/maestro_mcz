# maestro_mcz — transport Bluetooth local (fork ParalaX)

Trace de travail + spécification du portage BLE de l'intégration Home Assistant
[`Robbe-B/maestro_mcz`](https://github.com/Robbe-B/maestro_mcz) (aujourd'hui 100 % cloud MCZ)
vers un contrôle **local Bluetooth** (BLE via le Bluetooth de l'hôte HA / Raspberry Pi),
en **coexistence** avec le cloud (mode hybride, cloud gardé en backup).

> Ce fichier est la mémoire du projet. Il voyage avec le code (il sera poussé sur le fork).
> Base upstream figée : commit `a2ba9cf` (branche `main`), branche de travail `ble-local-transport`.

## 1. Contexte & sources

- Le protocole BLE des poêles MCZ Maestro a été reverse-engineeré dans l'issue upstream
  [#215 « Dump maestro+ »](https://github.com/Robbe-B/maestro_mcz/issues/215).
- Firmware ESP32 de référence (implémente TOUT le protocole) :
  [`foyewmaddeeb/mcz-maestro-ble`](https://github.com/foyewmaddeeb/mcz-maestro-ble) —
  copies locales dans `reference_ble/firmware_*.{h,cpp}`.
- Client Python BLE fonctionnel (base directe du port, `bleak`) :
  [`ParalaXEngineering/MCZ_Maestro_BLE`](https://github.com/ParalaXEngineering/MCZ_Maestro_BLE) —
  copie locale `reference_ble/mcz_ble_client.py` + doc protocole `reference_ble/ble-protocol.md`.

## 2. Cible matérielle (poêle de Matthieu)

| Point | Valeur | Impact |
|---|---|---|
| Modèle | MCZ **RAY** Comfort Air, **AIR** (pas hydro) | pas de bloc caldaia/puffer |
| Ventilos d'ambiance | **2** | voir §6 : le BLE n'a qu'UN registre de contrôle ventilo |
| Version FW/panel | **21.19** | constantes crypto à valider sur ce poêle (cf. §7 R-crypto) |
| Plateforme HA | **HAOS** (`http://10.0.4.200:8123`) | Bluetooth natif OK via l'intégration `bluetooth` |
| Distance Pi↔poêle | **~5 m, 2 murs** | limite du BT intégré → supporter les proxys BLE ESPHome |
| Déjà appairé à l'app MCZ | **Oui** | annonce probablement *directed* → mode appairage requis à l'install |
| Mode voulu | **Hybride cloud + BLE** (backup) | cloud = profil modèle + repli ; BLE = état + commandes locales |

## 3. Protocole BLE (résumé — détail complet dans `reference_ble/ble-protocol.md`)

- Le poêle annonce en `MCZ_EP<serial>`. GATT :
  - service `0xABF0` ; write-no-response `0xABF1` (commandes IN) ; notify `0xABF2` (réponses + push OUT).
  - **MTU 517**.
- Enveloppe **AES-128-CBC à IV FIXE** (jamais chaîné), constantes globales de cette génération FW :
  - `KEY1  = 6e296b0bbb1d43f36e47f72e7b6f2e77`
  - `IV0   = da1a557349f25c641b1a368af5b218a7`
  - `TOKEN = 31dd34512639377b05a2510de725fc75`
- Trame en clair (avant chiffrement) : `[compteur 4o LE][TOKEN 16o][PDU Modbus][CRC16 Modbus 2o LE][PKCS#7]`,
  taille toujours multiple de 16. Compteur +1 par message (valeur de départ `0x6A418000`).
- **Modbus RTU**, esclave `0x01` : Fn `0x03` = read holding registers, Fn `0x06` = write single,
  Fn `0x10` = write multiple (horloge). CRC16 poly `0xA001`, appended LE.
- Réponse Fn03 : `01 03 <byteCount> <data…> <crc>` — **ne contient PAS l'adresse** → mémoriser la base
  du dernier read (une seule lecture en vol à la fois).
- Push non sollicité `##` sur `0xABF2` : préfixe `23 23 <type> <frag>`, **fragmenté** (frag `01` puis `02`),
  AES même clé/token ; longueur JAMAIS multiple de 16 (discriminant vs trame Modbus). Après réassemblage
  + déchiffrement + check token → image registre à partir de `STATUS_BASE = 0x02BA`.
- **Appairage** : Just Works (IOCap NoInputNoOutput), LE Secure Connections, bonding. Sur BlueZ (HAOS) le
  bonding est géré par l'OS. Un poêle déjà bondé à un téléphone n'annonce qu'en directed → il faut
  **entrer en mode appairage** : appui simultané **+** et **−** sur l'afficheur, OU coupure secteur
  quelques secondes (après reboot l'afficheur est aussi en pairing). C'est l'action qui ouvre la
  « readiness gate » du poêle (détail : `reference_ble/readiness-gate.md`).

## 4. Carte des registres (vérifiée firmware + client Python)

### Écritures (commandes) — Fn `0x06`
| Registre | Sens | Valeur |
|---|---|---|
| `0x03F7` | Consigne température | °C × 10 (210 = 21.0 °C), bornes 5..45 °C |
| `0x03EB` | Puissance | 1..5 |
| `0x03E9` | Mode | 0=Manual, 1=Auto, 2=Overnight, 3=Comfort\*, 4=Turbo\* |
| `0x038A` | On/Off | écrire `1` = **toggle** (bouton power) — lire `0x0322` d'abord pour décider |
| `0x03FA` | Ventilo | 1..5 fixe, 6 = Auto |
| `0x03EC` | Silence | 1 = on, 0 = off |
| `0x0384`–`0x0388` | Horloge (Fn `0x10` bloc) | voir `firmware_oven.h` (binaire, non BCD) |

\* Modes 3/4 **supposés**, à confirmer sur le poêle réel.

### Lectures (état) — Fn `0x03`. Bloc principal de l'app : read `0x02BC` count `0x33` (51 reg).
| Registre | Sens | Conv. |
|---|---|---|
| `0x02BC` | Température ambiante | ÷10 |
| `0x02C1` | Température carte | ÷10 |
| `0x02C5` | Température fumées | ÷10 |
| `0x02C9` | « Active » | — |
| `0x02CE` | RPM ventilo combustion | RPM |
| `0x02D1` | RPM extracteur fumées | RPM |
| `0x0320` | État fin (code, voir `stateName`) | table |
| `0x0322` | Phase grossière | 1=Off, 3=On |
| `0x0324` | Niveau ventilo live | 1..5 |
| `0x032E` | Miroir mode live | 0..4 |
| `0x0332` | Flags | bit6=Chrono, bit5=Silence |
| `0x0334` | Compteur allumages | — |
| `0x0336`–`0x033F` | Temps par puissance 1..5 | 5× 32-bit sec |
| `0x0340`/`0x0341` | Temps total fonctionnement | 32-bit sec |
| `0x03F7` | Consigne (lue) | ÷10 |
| `0x0ADC` | N° série (ASCII, plusieurs reg) | → identité |

Détection capacités (`capScan`) : hydro `0x040F`, boiler `0x0407`, puffer `0x0412`, clima `0x0372`,
nb ventilos `0x06F4/0x06F5`, nb vitesses `0x05FD-0x05FF`, tipo_app `0x06A4`, banca_dati `0x07C6`.
Chrono (lecture) : `0x097C/0x097D` (temp eco/comfort), `0x097E + (D-1)*48 + (N-1)` (programme hebdo).

### États fins `0x0320`
`0x0000` Off · `0x0101` Cleaning · `0x0201` Loading · `0x0301` Start 1 · `0x0401` Start 2 ·
`0x0501` Stabilization · `0x0601` Anti-condensation · `0x0202` On · `0x0103` Turning off.

## 5. Architecture du fork

Le couplage des 10 plateformes (climate/fan/sensor/switch/select/number/button/datetime/binary_sensor)
passe **entièrement** par `MaestroStove` + le contrôleur `MaestroControllerInterface` (8 méthodes) +
les dataclasses `State`/`Status`/`Model`. **On ne touche pas aux plateformes ni à `MaestroStove`.**
On ajoute des contrôleurs.

Astuce clé : `State(dict, from_mocked_response=True)` / `Status(...)` font un simple `setattr` par
**nom d'attribut Python** → le contrôleur BLE construit ses dataclasses comme `MockedController`,
sans passer par les clés JSON de l'API cloud.

### Composants à créer (dans `custom_components/maestro_mcz/maestro/ble/`)
1. **`protocol.py`** — cœur réutilisable (port de `mcz_ble_client.py`) : CRC16, AES-CBC IV fixe,
   `build_frame`, PDU read/write, `parse_frame`, décodage réponse Fn03, réassemblage `##`.
   Constantes `KEY1/IV0/TOKEN`, UUIDs, `REG` map, `STATES`, `MODES`. **Zéro dépendance HA** (testable seul).
2. **`registers.py`** — la carte registres (§4) + fonctions de conversion + mapping
   `registre → attribut State/Status` et `attribut/commande → (registre, encodage)`.
3. **`transport.py`** — connexion BLE **via l'API Bluetooth de HA** (PAS `bleak` direct) :
   `bluetooth.async_ble_device_from_address()` + `bleak_retry_connector.establish_connection()`
   (compatible proxy ESPHome). Scan MCZ_EP, subscribe `0xABF2`, envoi `0xABF1`, image registre live,
   `read_regs(reg,count)`, `write_reg(reg,val)`, `capScan`. Gère reconnexion.
4. **`ble_controller.py`** — `MaestroBleController(MaestroControllerInterface)` : mode **BLE-only**.
   - `retrieve_linked_stove_infos()` → 1 `StoveInfo` (série `0x0ADC`→UniqueCode, MAC→identité).
   - `get_stove_state_for_stove` / `get_stove_status_for_stove` → dict par attribut → `State/Status(...,True)`.
   - `get_stove_model_for_stove` → **Model synthétisé minimal** depuis `capScan` (climate, power, fan, silent,
     temps). Suffisant en secours ; en hybride le Model vient du cloud.
   - `activate_program_with_commands_for_stove` → résout sensor→registre → écriture Fn06 (+ logique
     toggle on/off : lire `0x0322` puis basculer si besoin).
5. **`hybrid_controller.py`** — `MaestroHybridController(MaestroControllerInterface)` : **mode principal**.
   - Enveloppe un `MaestroController` (cloud) + un transport BLE.
   - `Model` ← **cloud** (profil réel, 2 fans corrects).
   - `State/Status` ← **BLE** si lien BLE actif, sinon **cloud** (repli).
   - Commandes ← **BLE** si le registre cible est connu (résolution sensor_id→sensor_name via le Model
     cloud → registre), sinon **cloud** (ex : 2e ventilo, reset alarme…).

### Config flow (`config_flow.py`)
Menu de choix de transport : **Bluetooth (local)** / **Cloud** / **Hybride**.
- Bluetooth/Hybride : découverte par advertising (`async_step_bluetooth`) + fallback liste
  `async_discovered_service_info` filtrée `MCZ_EP*` ; `unique_id` = MAC ; **étape d'instruction pairing**
  (« mettre l'afficheur en mode appairage : + et − simultanés, ou couper l'alim quelques secondes »).
- Hybride/Cloud : identifiants `username`/`password` (inchangés).
- `manifest.json` : ajouter `dependencies:["bluetooth_adapters"]`, `bluetooth:[{"local_name":"MCZ_EP*"}]`,
  `requirements:["bleak-retry-connector>=..."]` (crypto via `cryptography`, déjà dans HA),
  `iot_class` → `local_push` (broadcasts `##`) ou `local_polling`.
- `__init__.py` : instancier le bon contrôleur selon `entry.data["transport"]`.

### Polling / push
- S'abonner aux notify `0xABF2` (push `##` quasi temps réel) + polling léger (cycle ~2.5 s/bloc) pour les
  blocs non couverts (mode/puissance/consigne `0x03E9`, série `0x0ADC`). Une seule lecture Fn03 en vol.
- `update_data_after_set` (2×sleep(3) cloud) → réduire fort en BLE (écho Fn06 quasi immédiat).

## 6. Ventilos — verdict (Q de Matthieu)
Vitesse ventilo d'ambiance **lisible** (`0x0324`) **et pilotable** (`0x03FA` : 1..5 / 6=Auto) en BLE,
indépendamment du profil cloud. Bonus lecture seule : RPM combustion `0x02CE`, RPM fumées `0x02D1`.
**Limite** : un SEUL registre de contrôle ventilo côté BLE (pas de v1/v2/v3 séparés). Le RAY a **2 ventilos** :
→ en **hybride**, exposer les 2 entités fan (profil cloud), router fan#1 sur BLE (`0x03FA`) et fan#2 sur
le **cloud** tant qu'un registre BLE per-ventilo n'est pas identifié (foyewmaddeeb : « beaucoup de registres
non reconnus » — à reverser en live sur le poêle plus tard).

## 7. Risques ouverts (à lever sur le poêle réel)
- **R-pair** : bonding perdu au reboot Pi/poêle → re-appairage manuel (+/−). À tester.
- **R-exclu** : le poêle n'accepte probablement qu'UNE connexion BLE → si HA tient le lien, l'app MCZ ne
  pourra plus se connecter en BLE. Le cloud reste dispo en parallèle (mode hybride).
- **R-portee** : BT du Pi à ~5 m/2 murs = limite → prévoir proxy BLE ESPHome si instable.
- **R-crypto** : `KEY1/IV0/TOKEN` réputées globales mais issues d'un dump ; valider le déchiffrement sur
  le FW 21.19 du poêle.
- **R-mode34** : codes mode Comfort(3)/Turbo(4) supposés, à confirmer.
- **R-onoff** : pas d'écriture d'état absolu connue, seulement toggle `0x038A` → lire phase avant.
- **R-fan2** : 2e ventilo non pilotable en BLE (voir §6).
- **R-cmd-absentes** : reset alarme, Start/Stop Eco, buzzer, standby delays : pas de registre identifié
  → router au cloud en hybride, ne pas créer d'entité BLE fantôme.

## 8. Livraison / test
- Cible : installer via **HACS** (custom repository) depuis le fork `ParalaXEngineering/maestro_mcz`.
- HAOS n'expose pas d'écriture fichier `custom_components/` par l'API : l'upload direct via MCP n'est
  a priori **pas** possible → passage par GitHub + HACS (à confirmer dans le status).
- Accès HA de Matthieu dispo via MCP `ha-mcp` (10.0.4.200) pour vérifs/diagnostic une fois installé.

## 9. Cible réelle vérifiée (via l'API HA, 2026-08-20)

| Élément | Valeur |
|---|---|
| HA core | 2026.8.2 — **Python 3.14.6** |
| OS | Home Assistant OS 18.2, Supervisor 2026.07.5 |
| Carte | **Raspberry Pi 5** (`rpi5-64`), Bluetooth intégré |
| HACS | 2.0.5 (installation par dépôt personnalisé possible) |
| Add-ons utiles | ESPHome Device Builder (→ proxy BLE si portée insuffisante), Mosquitto, Terminal & SSH |

Python 3.14 confirme que la syntaxe `except A, B:` d'origine passait ; elle a quand même été
parenthésée (portabilité, aucun changement de comportement).

## 10. Code livré (branche `ble-local-transport`)

Nouveau paquet `custom_components/maestro_mcz/maestro/ble/` :

| Fichier | Rôle |
|---|---|
| `protocol.py` | Portage pur du client de référence : CRC16, AES-CBC IV fixe, trames, PDU Modbus, réassembleur `##`. Aucune dépendance HA → testable seul. |
| `registers.py` | Carte registres, conversions, `REG_TO_STATE`/`REG_TO_STATUS`, `WRITE_MAP`, `parse_capabilities`, reconstruction `stato_stufa`/`fase_op`, `decode_onoff`/`needs_onoff_toggle`. |
| `transport.py` | Lien BLE via l'API Bluetooth de HA (`async_ble_device_from_address` + `bleak_retry_connector`) → compatible proxy ESPHome. Image registre live, une lecture Fn03 en vol, notify `0xABF2`. |
| `ble_controller.py` | Mode **BLE-only** : implémente l'interface, `Model` synthétisé minimal depuis `capScan`. |
| `hybrid_controller.py` | Mode **principal** : `Model`+base d'état du cloud, **surcouche** BLE fraîche, commandes en BLE si le registre est connu sinon cloud. |

Modifiés : `manifest.json` (dependencies `bluetooth_adapters`, matcher `MCZ_EP*`, requirement
`bleak-retry-connector`, `iot_class: local_push`), `const.py`, `config_flow.py` (menu cloud / BLE /
hybride + découverte + étape d'appairage), `__init__.py` (choix du contrôleur), `strings.json` +
`translations/en.json`.

### Défauts trouvés en revue et corrigés
1. **Toggle on/off aveugle (risque de sûreté)** — `0x038A` est un *toggle* : le code écrivait le
   toggle sans comparer à l'état demandé → « éteindre » un poêle déjà éteint l'**allumait**.
   Corrigé par `decode_onoff` + `needs_onoff_toggle` (parité avec `ovenSetOnOff` du firmware).
2. **Régression de couverture en hybride** — l'état BLE *remplaçait* l'état cloud, laissant à `None`
   toutes les entités non couvertes en BLE (2e ventilo, eco, buzzer, maintenance…). Corrigé :
   fusion `cloud (base) + BLE (surcouche)` via `_overlay`.
3. **Hybride non résilient au cloud** — une panne cloud rendait tout indisponible malgré un BLE actif.
   Corrigé : ping cloud non fatal si le BLE répond, `is_authenticated = cloud OR ble`.
4. **Réponse Fn03 tardive** — une réponse arrivant après timeout était décodée avec la base du *read
   suivant* → registres corrompus. Corrigé par un drapeau `_read_pending`.
5. `except A, B:` parenthésé (portabilité).

### Tests hors ligne passés
Aller-retour de trame (`010603f700d2` → chiffrée 32 o → déchiffrée, token + CRC + PKCS#7),
PDU de lecture `010302bc0033`, logique on/off (les 6 cas), construction `State`/`Status`
(21.3 °C, state `On`, `stato_stufa=3`), capacités (air, 2 ventilos, 5 vitesses), `encode_write`,
réassembleur `##` (2 fragments → 8 registres).

## 11. Journal
- **2026-08-20** — Analyse complète des 3 repos + issue #215 (workflow 5 agents, vérifié source).
  Décisions : fork en place (domaine `maestro_mcz` conservé), mode **hybride** cloud+BLE, transport via
  API Bluetooth HA, `State/Status` BLE via `from_mocked_response`, `Model` cloud en hybride /
  synthétisé minimal en BLE-only.
- **2026-08-20** — Code écrit (workflow codeur + vérificateur adverse), 4 défauts corrigés dont un
  risque de sûreté sur l'on/off. Tests hors ligne verts. Revue finale Opus 5 lancée.
  **Pas encore installé sur HA** — en attente de validation avant publication du fork.
