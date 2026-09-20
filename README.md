# Peer Reviewer

Peer Reviewer prowadzi kontrolowaną debatę dwóch niezależnych recenzentów —
Claude Code i Codex — nad jednym, niezmiennym dokumentem. Deterministyczny silnik
zarządza rundami, pilnuje limitów subskrypcji, zapisuje historię i generuje raport,
nie używając płatnych kluczy API.

> [!WARNING]
> Projekt nie jest jeszcze gotowy do użycia produkcyjnego. Implementacja offline
> i testy z atrapami działają, ale integracja z prawdziwymi kontami OAuth oraz
> odczyt limitu Claude nie zostały potwierdzone. Obecne obrazy workerów nie
> instalują jeszcze binariów Claude Code ani Codex. Szczegóły znajdują się w
> [docs/feasibility.md](docs/feasibility.md).

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

Pełne uruchomienie z prawdziwymi recenzentami będzie dodatkowo wymagało
zweryfikowanych wersji Claude Code CLI i Codex CLI oraz osobnego logowania OAuth
dla każdego workera.

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

Następnie ustaw właściwe modele, wersje CLI i identyfikatory kont w
`config/reviewer.toml`. Wartości `PIN_AFTER_LIVE_PREFLIGHT` oraz `unconfigured`
są celowymi placeholderami i nie nadają się do wdrożenia.

Przed rozpoczęciem sesji sprawdź konfigurację:

```bash
uv run peer-reviewer doctor \
  --config config/reviewer.toml \
  --sessions sessions
docker compose config --quiet
```

Nie uruchamiaj prawdziwej recenzji, dopóki macierz testów opisana w
[docs/feasibility.md](docs/feasibility.md) nie zakończy się powodzeniem dla obu
dostawców.

## Docelowe uruchomienie w Dockerze

Po uzupełnieniu obrazów workerów o zweryfikowane CLI, przygotowaniu OAuth
i poprawnej konfiguracji przebieg będzie wyglądał następująco:

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
  niekontrolowane zużycie.

## Dokumentacja

- [Instrukcja operacyjna](docs/operations.md)
- [Stan wykonalności integracji](docs/feasibility.md)
- [Wymagania produktu](dual-reviewer-wymagania.md)
- [Plan techniczny](docs/superpowers/plans/2026-09-19-dual-reviewer.md)

## Stan projektu

Rdzeń, trwały zapis, sterowanie CLI i scenariusze awarii mają pokrycie testami
offline. Produkcyjne **GO** wymaga nadal potwierdzenia działania obu CLI w
docelowych kontenerach, logowania OAuth, świeżych odczytów limitów oraz
rzeczywistego przebiegu recenzji.
