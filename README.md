# Peer Reviewer

Peer Reviewer prowadzi kontrolowaną debatę dwóch niezależnych recenzentów —
Claude Code i Codex — nad jednym, niezmiennym dokumentem. Deterministyczny silnik
zarządza rundami, pilnuje limitów subskrypcji, zapisuje historię i generuje raport,
nie używając płatnych kluczy API.

> [!WARNING]
> Odczyt limitów obu dostawców działa bez generowania tokenów: Claude przez
> `claude -p /usage`, Codex przez App Server `account/rateLimits/read`. Prawdziwe
> tury obu CLI przechodzą walidację protokołu. Przed produkcją pozostają:
> logowanie OAuth w wolumenach workerów, wyłączenie płatnego „extra usage” na obu
> kontach i przypięcie modeli. Szczegóły są w [docs/feasibility.md](docs/feasibility.md).

## Jak to działa

Projekt uruchamia trzy odizolowane usługi:

- `engine` steruje sesją, utrwala rundy i generuje raport;
- `claude` obsługuje recenzenta A przez Claude Code CLI;
- `codex` obsługuje recenzenta B przez Codex CLI.

Każdy recenzent otrzymuje ten sam zamrożony stan rundy. Wyniki są walidowane,
a ukończone rundy zapisywane w katalogu sesji. Użytkownik steruje procesem przez
CLI i może zatrzymać, wznowić albo zaakceptować wynik.

## Wymagania

Do pracy nad projektem potrzebne są:

- Python 3.14 lub nowszy;
- [uv](https://docs.astral.sh/uv/);
- Docker z obsługą Compose — do testów i docelowego uruchomienia kontenerów.

Obrazy workerów instalują przypięte wersje CLI: Claude Code 2.1.280 i Codex
0.156.1. Każdy worker ma osobny wolumen OAuth.

## Uruchomienie lokalne

Zainstaluj zależności zgodnie z plikiem `uv.lock`:

```bash
uv sync
```

Sprawdź dostępne polecenia:

```bash
uv run peer-reviewer --help
```

Uruchom testy offline:

```bash
uv run pytest -q
```

Testy oznaczone jako `live` wymagają jawnie przygotowanych, prawdziwych kont
i nie są uruchamiane w zwykłym zestawie testów.

## Konfiguracja

Skopiuj przykładową konfigurację:

```bash
cp config/example.toml config/reviewer.toml
```

Zaloguj oba CLI, każde do własnego wolumenu (jednorazowo, interaktywnie):

```bash
docker compose build
docker compose run --rm --entrypoint claude claude auth login --claudeai
docker compose run --rm --entrypoint codex codex login --device-auth
```

Odczytaj limity i identyfikatory kont:

```bash
docker compose run --rm claude --probe
docker compose run --rm codex --probe
```

Wartość `observed_account_fingerprint` wpisz jako `account_fingerprint`
w `config/reviewer.toml`. To skrót konta, nie sekret. Ustaw też jawne modele
w miejsce `PIN_AFTER_LIVE_PREFLIGHT`.

Przed rozpoczęciem sesji sprawdź konfigurację:

```bash
docker compose up -d
docker compose exec -T engine peer-reviewer doctor \
  --config /config/reviewer.toml --sessions /sessions
```

`doctor` odpytuje działające workery o świeży odczyt limitu, wersję CLI i konto.

## Docelowe uruchomienie w Dockerze

Po zalogowaniu OAuth i uzupełnieniu konfiguracji:

```bash
mkdir -p input sessions
cp document.md input/document.md
docker compose up -d
docker compose exec -T engine \
  peer-reviewer start /input/document.md --session /sessions/s-001
```

Compose montuje domyślnie:

- `./input` jako katalog dokumentów tylko do odczytu;
- `./sessions` jako trwały katalog sesji;
- `./config` jako konfigurację tylko do odczytu.

Ścieżki można zmienić przez zmienne `PEER_REVIEWER_INPUT`,
`PEER_REVIEWER_SESSIONS` i `PEER_REVIEWER_CONFIG`.

## Obsługa sesji

Podane niżej komendy można uruchamiać lokalnie przez `uv run` albo wewnątrz
kontenera `engine` przez `docker compose exec -T engine`.

```bash
# Stan sesji
uv run peer-reviewer status --session sessions/s-001

# Odświeżany podgląd stanu
uv run peer-reviewer status --session sessions/s-001 --watch

# Zatrzymanie i wznowienie
uv run peer-reviewer stop --session sessions/s-001
uv run peer-reviewer resume --session sessions/s-001

# Wygenerowanie i zaakceptowanie raportu
uv run peer-reviewer report --session sessions/s-001
uv run peer-reviewer accept --session sessions/s-001
```

Zamknięcie terminala, połączenia SSH lub polecenia `status --watch` nie zatrzymuje
silnika działającego w Dockerze.

## Wyniki

Najważniejsze pliki sesji można czytać bez wchodzenia do kontenera:

```text
sessions/s-001/
├── runtime/status.md       # bieżący stan
├── rounds/0001/round.md    # wynik pierwszej rundy
└── report.md               # raport końcowy
```

Do wykonania kopii zapasowej zachowaj cały katalog sesji. Sam `report.md` nie
zawiera kompletnej historii potrzebnej do odtworzenia procesu.

## Bezpieczeństwo i koszty

- Każdy provider używa osobnego wolumenu OAuth.
- Projekt nie przekazuje kluczy API do kontenerów.
- Przed wdrożeniem wyłącz dodatkowo płatne użycie, kredyty i fallback API na obu
  kontach dostawców.
- Nie montuj w kontenerach katalogu domowego hosta, kluczy SSH, magazynu sekretów
  ani gniazda Dockera.
- Brak świeżych danych o limicie blokuje kolejną rundę zamiast ryzykować
  niekontrolowane zużycie. Odczyt limitu nie zużywa limitu.

## Dokumentacja

- [Stan prac i lista do zrobienia](docs/status-2026-09-23.md)
- [Instrukcja operacyjna](docs/operations.md)
- [Stan wykonalności integracji](docs/feasibility.md)
- [Wymagania produktu](dual-reviewer-wymagania.md)
- [Plan techniczny](docs/superpowers/plans/2026-09-19-dual-reviewer.md)

## Stan projektu

Rdzeń, trwały zapis, sterowanie CLI i scenariusze awarii mają pokrycie testami
offline. Odczyt limitów i pojedyncze tury recenzji zweryfikowano na prawdziwych
kontach na hoście. Do produkcyjnego **GO** brakuje logowania OAuth w kontenerach,
wyłączenia płatnego użycia na kontach i przypięcia modeli.
