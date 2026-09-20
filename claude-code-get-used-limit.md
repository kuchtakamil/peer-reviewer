Dobra wiadomość: **to pole istnieje i jest oficjalnie udokumentowane** — dokładnie w postaci, o którą pytasz (procent + czas resetu). Zła: kanał dostarczania jest zaprojektowany pod tryb interaktywny, a nie pod `-p`.

## Źródło prawdy: JSON status line

Claude Code przekazuje skryptowi status line JSON na stdin, a w nim m.in.:

| Pole | Znaczenie |
|---|---|
| `rate_limits.five_hour.used_percentage` | procent zużytego limitu okna 5-godzinnego, od 0 do 100 |
| `rate_limits.five_hour.resets_at` | czas resetu okna w sekundach epoch Unix |
| `rate_limits.seven_day.*` | to samo dla okna tygodniowego |

Przykładowy payload:

```json
"rate_limits": {
  "five_hour": { "used_percentage": 23.5, "resets_at": 1738425600 },
  "seven_day": { "used_percentage": 41.2, "resets_at": 1738857600 }
}
```

Odczyt jednolinijkowcem: `jq -r '.rate_limits.five_hour.used_percentage // empty'`.

**Trzy warunki obecności pola**, każdy istotny dla Twojego F-12: obiekt `rate_limits` pojawia się wyłącznie dla subskrybentów Claude.ai Pro i Max (albo za bramką Claude apps gateway z limitem wydatków) i dopiero po pierwszej odpowiedzi API w sesji. Każde okno może być nieobecne niezależnie, a Claude Code usuwa okno, gdy minie jego `resets_at`.

Czyli: brak pola ≠ brak limitu. Może znaczyć „sesja jeszcze nie wykonała żadnego zapytania". Twoja zachowawcza reguła z F-12 jest tu uzasadniona.

## Problem: kanał jest interaktywny

Status line to element UI — pasek na dole sesji. Skrypt uruchamia się raz przy starcie sesji (także przy wznowieniu), a potem ponownie, gdy nadejdzie nowa wiadomość asystenta, zakończy się `/compact`, zmieni się tryb uprawnień, przełączy tryb vim, zmienisz `command` w ustawieniach, upłynie timer `refreshInterval`, albo gdy okno rate-limitu w ostatnio otrzymanych danych osiągnie swój `resets_at`.

W dokumentacji trybu headless (`-p`) **nie ma ani słowa o `rate_limits`**. Payload wyniku `--output-format json` daje koszt i tokeny — z `--output-format json` odpowiedź zawiera `total_cost_usd` i rozbicie kosztów per model, więc skryptowy wywołujący może śledzić wydatek na wywołanie bez zaglądania do dashboardu — ale to szacunek lokalny per sesja, nie stan okna planu.

Czy statusLine odpala się pod `-p`? **Nie jest to udokumentowane w żadną stronę.** Nie zgaduję — masz to sprawdzić eksperymentalnie, i to jest test na dwie minuty:

```bash
cat > ~/.claude/limits-probe.sh <<'EOF'
#!/bin/bash
jq -c '{ts: now, rl: .rate_limits}' >> /tmp/cc-limits.jsonl
echo ""
EOF
chmod +x ~/.claude/limits-probe.sh
# wpisz do ~/.claude/settings.json:  "statusLine": {"type":"command","command":"~/.claude/limits-probe.sh"}

rm -f /tmp/cc-limits.jsonl
claude -p "say hi" >/dev/null
cat /tmp/cc-limits.jsonl     # pusty → statusLine nie działa w -p
```

Jeśli plik urośnie — masz rozwiązanie idealne: każda runda recenzji sama aktualizuje odczyt limitu jako efekt uboczny, zero dodatkowych tokenów. Uwaga: nie używaj wtedy `--bare`, bo pomija on autodetekcję hooków, skilli, komend, subagentów, pluginów, serwerów MCP, auto-memory i CLAUDE.md, a status line podlega tym samym bramkom co hooki.

## Ścieżki zapasowe, gdyby test wypadł negatywnie

**1. Sesja-sonda w tmux.** Jedna długo żyjąca sesja interaktywna w kontenerze, wyłącznie jako czujnik, ze statusLine zapisującym JSON do pliku i `refreshInterval`. Wada: dane odświeżają się przy odpowiedziach API *tej* sesji, więc między rundami odczyt się starzeje. Trzeba by ją szturchać trywialnym promptem — tani, ale nie darmowy.

**2. Reaktywnie, zamiast prewencyjnie.** W `stream-json` Claude Code emituje zdarzenie `system/api_retry` z polem `error`, którego kategorie obejmują m.in. `rate_limit`, `billing_error` i `overloaded`. To mówi „już wtopiłeś", nie „zostało Ci 18%" — ale jako backstop jest niezawodne.

**3. Wbudowane czekanie zamiast własnego.** Na v2.1.234+ istnieje mechanizm poczekania i automatycznego kontynuowania przerwanego zadania po resecie, sterowany ustawieniem `autoContinueAtUsageLimit`. To jest jednak funkcja sesji interaktywnej — pod `-p` nie licz na nią.

**4. Własny licznik z `total_cost_usd`.** Sumujesz koszt per runda i sam szacujesz spalanie. Zasadnicza wada: okno 5h jest wspólne z claude.ai, Cowork i innymi urządzeniami, więc Twój licznik nie widzi połowy zużycia.

**5. `/usage` w `-p`** — raczej nie. Wbudowane komendy działające tylko w interfejsie terminala nie są dostępne w trybie `-p`, a lista wyjątków (`/model`, `/effort`, `/fast`, `/color`, `/rename`, `/mcp`) nie obejmuje `/usage`. Dodatkowo niektóre komendy jak `/usage` generują zapytania sprawdzające status — odpytywanie limitu samo zużywa limit, więc nie rób z tego pętli pollingowej.

## Co to znaczy dla wymagań

Twoje F-8…F-12 są realizowalne, ale zapisz je odporniej: nie „odczytaj procent", tylko **dwupoziomowo** — prewencyjnie z `rate_limits` gdy jest dostępny, reaktywnie z `api_retry`/`rate_limit` gdy go nie ma. Przy braku odczytu system i tak nie ryzykuje: rusza z rundą, a jeśli oberwie limitem, wchodzi w pauzę do `resets_at` z ostatniego znanego odczytu (albo na sztywne 5h od pierwszego błędu).

Jedna rzecz, której tu nie zbadałem, bo pytałeś tylko o Claude Code: **strona Codeksa to osobne pytanie i prawdopodobnie inna odpowiedź.** Bez symetrycznego odczytu po obu stronach F-8 zostaje spełnione połowicznie — a Twój warunek mówi „po obu stronach osobno".