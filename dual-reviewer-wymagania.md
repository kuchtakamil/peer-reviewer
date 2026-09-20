# Dual Reviewer — co chcę uzyskać

**Status:** opis wymagań (nie plan implementacji)
**Data:** 2026-09-13

---

## 1. Cel

Chcę mieć system, któremu podaję tekst techniczny do recenzji, a on przeprowadza
**iteracyjną debatę między dwoma niezależnymi agentami** — Claude Code i Codex —
aż do osiągnięcia konsensusu, i zwraca mi uzgodnioną listę uwag.

Zależy mi na tym, że dwa różne modele od dwóch różnych dostawców mają różne
"ślepe plamy". To, co przetrwa konfrontację obu, jest warte mojej uwagi.
To, co jeden zgłosi, a drugi obali, chcę zobaczyć jako odrzucone — wraz z powodem.

Wynikiem nie jest "dwa raporty do porównania", tylko **jeden dokument
z uzgodnionym stanowiskiem i jawnie zaznaczonymi punktami spornymi.**

---

## 2. Twarde ograniczenia

| # | Ograniczenie | Uzasadnienie |
|---|---|---|
| O-1 | **Zero wywołań płatnego API** obu dostawców | Korzystam wyłącznie z abonamentów ~$20 (Claude + ChatGPT) |
| O-2 | Recenzenci działają jako **CLI w trybie nieinteraktywnym**, na własnym logowaniu OAuth | Jedyna droga do wykorzystania abonamentu zamiast klucza API |
| O-3 | Orkiestrator nie może w pętli palić tokenów trzeciego dostawcy | Inaczej "oszczędność na API" jest pozorna |
| O-4 | Całość uruchamiana w **Dockerze** — lokalnie albo na moim VPS | Izolacja od maszyny roboczej; VPS pozwala na pracę bez laptopa |

---

## 3. Aktorzy

- **Recenzent A** — Claude Code
- **Recenzent B** — Codex
- **Orkiestrator** — Hermes Agent (kontener Docker, lokalnie lub na VPS)
- **Człowiek** — ja; decyduję o starcie, mogę przerwać po każdej rundzie,
  akceptuję wynik końcowy

Recenzenci są **symetryczni**. Żaden nie jest przełożonym drugiego i żaden
nie decyduje samodzielnie o osiągnięciu konsensusu.

---

## 4. Przebieg — czego oczekuję

```
   tekst wejściowy
        │
        ▼
   ┌─ RUNDA N ────────────────────────────────┐
   │                                          │
   │  [0] sprawdź pozostały limit 5h obu stron│
   │       └─ < 20% ? → PAUZA do resetu       │
   │                                          │
   │  [1] Recenzent A: odnieś się do stanu    │
   │       debaty, zgłoś/podtrzymaj/wycofaj   │
   │                                          │
   │  [2] Recenzent B: to samo, wobec A       │
   │                                          │
   │  [3] zapisz stan rundy na dysk (.md)     │
   │                                          │
   │  [4] punkt kontrolny człowieka           │
   │       └─ przerwać ? → STOP (stan zapisany│
   │                                          │
   │  [5] konsensus ? → KONIEC : RUNDA N+1    │
   └──────────────────────────────────────────┘
```

Kolejność kroków 0 → 3 → 4 jest istotna: limit sprawdzam **przed**
poniesieniem kosztu, a zapis robię **przed** oddaniem decyzji człowiekowi.

---

## 5. Wymagania funkcjonalne

### 5.1 Debata

- **F-1** — Runda składa się z dokładnie jednej wypowiedzi każdego recenzenta.
- **F-2** — Recenzent w rundzie N > 1 musi **odnieść się do konkretnych uwag
  drugiej strony**, a nie pisać recenzji od nowa. Dopuszczalne stanowiska wobec
  każdej uwagi: zgoda / sprzeciw / modyfikacja propozycji.
- **F-3** — Zmiana stanowiska wymaga wskazania argumentu, który ją spowodował.
  Chcę uniknąć grzecznościowej kapitulacji w rundzie drugiej.
- **F-4** — Recenzenci nie mają dostępu do zapisu poza katalogiem roboczym
  sesji i nie modyfikują recenzowanego tekstu.

### 5.2 Warunek zakończenia

- **F-5** — Konsensus musi być **rozstrzygalny mechanicznie**, a nie na
  podstawie oceny "wygląda na to, że się zgadzają".
- **F-6** — Twardy limit rund (domyślnie 5). Po jego przekroczeniu praca kończy
  się statusem "brak konsensusu" i listą pozycji spornych — to też jest wynik.
- **F-7** — Pozycje sporne po ostatniej rundzie muszą trafić do raportu
  ze stanowiskiem obu stron, nie zostać po cichu odrzucone.

### 5.3 Zarządzanie limitami abonamentu

- **F-8** — Przed każdą rundą system sprawdza, ile pozostało z 5-godzinnego
  okna limitów **po obu stronach osobno**.
- **F-9** — Jeżeli którakolwiek strona ma **poniżej 20%** pozostałego limitu,
  runda nie startuje. System przechodzi w stan PAUZA.
- **F-10** — W stanie PAUZA system czeka na reset okna i wznawia pracę
  automatycznie, z tego samego miejsca. Nie wymaga to mojej obecności.
- **F-11** — Chcę wiedzieć, że system śpi i do kiedy — stan pauzy musi być
  widoczny na zewnątrz (w pliku stanu i w powiadomieniu).
- **F-12** — Jeżeli odczyt limitu jest niedostępny lub niepewny, system
  zachowuje się zachowawczo (traktuje jak niski limit), zamiast ryzykować
  wyczerpanie okna w połowie rundy.

### 5.4 Kontrola człowieka

- **F-13** — Po każdej rundzie istnieje punkt, w którym mogę pracę **przerwać**.
- **F-14** — Przerwanie jest czyste: stan po ostatniej ukończonej rundzie
  zostaje zachowany, nic nie jest w połowie zapisane.
- **F-15** — Brak mojej reakcji w rozsądnym czasie **nie blokuje** procesu —
  domyślnie praca leci dalej. Punkt kontrolny to prawo weta, nie obowiązek
  siedzenia przy terminalu.
- **F-16** — Powiadomienie po rundzie powinno docierać do mnie poza terminalem
  (np. komunikator), skoro całość ma działać na VPS.

### 5.5 Trwałość i odporność na crash

- **F-17** — Wynik **każdej** rundy jest zapisywany do pliku `.md`
  natychmiast po jej zakończeniu. Nie na końcu całej debaty.
- **F-18** — Po crashu (kontener, sesja CLI, VPS, sieć) nie tracę niczego poza
  rundą, która była w trakcie.
- **F-19** — System potrafi **wznowić debatę** z zapisanego stanu i kontynuować
  od następnej rundy.
- **F-20** — Plik z przebiegiem jest czytelny dla człowieka bez narzędzi —
  mam móc go po prostu otworzyć i przeczytać, kto co powiedział i dlaczego.
- **F-21** — Historia jest **append-only**. Runda raz zapisana nie jest
  nadpisywana ani "porządkowana" w kolejnych rundach.

### 5.6 Wejście i wyjście

- **F-22** — Wejście: plik tekstowy (markdown lub zwykły tekst).
  Nie musi to być kod ani repozytorium git.
- **F-23** — Wyjście: katalog sesji z przebiegiem rund oraz raport końcowy
  zawierający uwagi uzgodnione, uwagi odrzucone (z powodem) i pozycje sporne.

---

## 6. Wymagania niefunkcjonalne

- **N-1 — Izolacja.** Kontener nie ma dostępu do moich kluczy SSH,
  vaulta Obsidian ani niczego poza katalogiem sesji i konfiguracją logowania
  obu CLI.
- **N-2 — Obserwowalność.** W dowolnym momencie potrafię odpowiedzieć na
  pytanie "na czym stoi i dlaczego" bez wchodzenia do kontenera.
- **N-3 — Determinizm sterowania.** Decyzje o starcie rundy, pauzie i
  zakończeniu podejmuje logika, nie model. Model recenzuje, nie zarządza.
- **N-4 — Brak wymogu TTY.** Całość działa bez emulacji terminala i bez
  klikania w dialogi.
- **N-5 — Idempotencja wznowienia.** Ponowne uruchomienie na istniejącej sesji
  nie duplikuje rund ani nie zaczyna od zera.
- **N-6 — Przenośność.** To samo uruchomienie lokalnie i na VPS, bez zmian
  w logice.

---

## 7. Poza zakresem (na razie)

- Więcej niż dwóch recenzentów.
- Automatyczne nanoszenie uzgodnionych poprawek na recenzowany tekst.
- Interfejs graficzny.
- Wersjonowanie recenzowanego dokumentu w czasie (kolejne wersje tekstu).
- Uruchamianie wielu sesji równolegle.

---

## 8. Kryteria akceptacji

Uznam projekt za udany, gdy:

1. Wrzucam plik z tekstem, wracam po godzinie i mam raport z konsensusem —
   bez ani jednego wywołania płatnego API.
2. Zabicie kontenera w połowie debaty kosztuje mnie jedną rundę, nie całą sesję.
3. System sam przeczekał reset limitu i dokończył pracę pod moją nieobecność.
4. W raporcie widzę przynajmniej jedną uwagę, którą jeden recenzent zgłosił,
   a drugi merytorycznie obalił — dowód, że to była realna debata,
   a nie dwa monologi obok siebie.

---

## 9. Otwarte pytania

- Jak wiarygodnie odczytać stan 5-godzinnego okna limitów po **obu** stronach?
  Czy oba CLI w ogóle to udostępniają w formie nadającej się do odczytu maszynowego?
- Czy przy braku konsensusu po N rundach chcę trzeciego, neutralnego arbitra,
  czy wolę dostać surowy spór do własnej decyzji?
- Jak długie mają być tury, żeby recenzent nie streszczał samego siebie
  w kółko przy dłuższych tekstach?

Powtarzaniu zapobiegałbym przede wszystkim formatem debaty: stałe identyfikatory uwag, odpowiedzi na konkretny argument, obowiązkowe uzasadnienie zmiany stanowiska i zakaz
ponownego streszczania całej recenzji. Recenzent powinien dostawać aktualny rejestr uwag, ostatnią wypowiedź drugiej strony oraz dostęp do tekstu źródłowego.

---

## 10. Alternatywny przebieg — niezależne recenzje i wspólna debata

**Status:** wariant do wyboru zamiast sekwencyjnego przebiegu z rozdziału 4.
Pozostałe ograniczenia i wymagania obowiązują również w tym wariancie;
poniższy opis doprecyzowuje sposób wymiany stanowisk i rozstrzygania uwag.

Pierwsza runda tworzy dwie niezależne recenzje. Druga konfrontuje wszystkie
zgłoszone uwagi, a kolejne służą rozstrzyganiu różnic i potwierdzaniu zmian.

### 10.1 Wspólny stan na początku każdej rundy

W każdej rundzie obaj recenzenci otrzymują **ten sam stan debaty** z końca
poprzedniej ukończonej rundy. Wypowiedź partnera z bieżącej rundy poznają
dopiero po zebraniu obu odpowiedzi. W pierwszej rundzie otrzymują wyłącznie
tekst źródłowy, kryteria recenzji i instrukcje procesu.

Recenzenci mogą pracować równocześnie albo kolejno. Warunkiem jest brak
dostępu do odpowiedzi partnera z bieżącej rundy, także przez pliki robocze.
Runda nadal składa się z dokładnie jednej wypowiedzi każdego recenzenta (F-1).

Ten wariant zachowuje symetrię: żaden recenzent nie odpowiada na nowszy stan
niż drugi. Ceną może być dodatkowa runda potrzebna do zaakceptowania zmiany
zaproponowanej przez partnera.

### 10.2 Przygotowanie sesji

System zapisuje niezmienną kopię dokumentu wejściowego. Obaj recenzenci
otrzymują identyczny tekst, zakres oceny i format uwag. Zakres może obejmować
np. poprawność techniczną, spójność argumentacji i brakujące założenia.

Każda zgłoszona uwaga zawiera:

- stały identyfikator i autora;
- wskazanie konkretnego fragmentu dokumentu;
- opis problemu i jego znaczenia;
- uzasadnienie;
- proponowaną poprawkę lub sposób rozwiązania problemu.

### 10.3 Runda pierwsza — niezależne recenzje

Claude Code i Codex osobno recenzują cały dokument, bez znajomości uwag
partnera. Po zebraniu obu wypowiedzi orkiestrator tworzy wspólny rejestr
wszystkich zgłoszeń, zachowując ich autorstwo i identyfikatory, np. A-1 i B-1.

Orkiestrator nie ocenia merytorycznie uwag ani nie scala ich na podstawie
podobieństwa. Jeżeli obaj zgłoszą problem dotyczący tej samej definicji,
obie pozycje pozostają w rejestrze do uzgodnienia ich związku przez recenzentów.

Wynik rundy zostaje zapisany przed powiadomieniem człowieka. Dwie niezależne
recenzje nie są jeszcze uzgodnionym raportem, nawet jeśli brzmią podobnie.

### 10.4 Runda druga — wzajemna weryfikacja

Obaj recenzenci otrzymują tekst źródłowy, obie pierwsze recenzje oraz wspólny
rejestr uwag. Każdy zajmuje jawne stanowisko wobec wszystkich pozycji:
potwierdza lub zmienia własne i ocenia zgłoszenia partnera.

Dopuszczalne są zgoda, sprzeciw lub propozycja modyfikacji. Sprzeciw wymaga
argumentu, modyfikacja — wskazania nowej treści i powodu zmiany. Recenzenci
mogą również zaproponować uznanie uwagi za duplikat innej pozycji.

Przykładowy wynik rundy:

| Uwaga | Claude Code | Codex | Stan po rundzie |
|---|---|---|---|
| A-1: błędna definicja dostępności | Podtrzymuje | Akceptuje uwagę i poprawkę | Uzgodniona |
| A-2: zbyt mocna gwarancja trwałości | Podtrzymuje | Odrzuca, wskazując założenie z tekstu | Sporna |
| B-1: pominięty koszt replikacji | Akceptuje uwagę i poprawkę | Podtrzymuje | Uzgodniona |
| B-2: nieprecyzyjna definicja dostępności | Proponuje połączenie z A-1 | Podtrzymuje osobno | Wymaga uzgodnienia |

Claude pozna kontrargument dotyczący A-2 dopiero po zakończeniu tej rundy.
Nie oczekuje się od niego odpowiedzi na argument, którego jeszcze nie otrzymał.

### 10.5 Runda trzecia i kolejne — odpowiedzi na kontrargumenty

Recenzenci otrzymują rejestr po poprzedniej rundzie, wypowiedzi obu stron
z tej rundy oraz dostęp do tekstu źródłowego i wcześniejszych argumentów.
Rozwijają kwestie otwarte. Przy pozycjach już uzgodnionych wystarczy jawne
potwierdzenie identyfikatora i zaakceptowanej wersji, bez powtarzania uzasadnień.

Zmiana stanowiska wymaga wskazania argumentu, który ją spowodował (F-3).
Na przykład autor A-2 może wycofać zarzut, ponieważ partner wskazał ograniczenie
gwarancji do awarii pojedynczego węzła, a pierwotna uwaga zakładała awarię
całego klastra. Może też podtrzymać zarzut i wyjaśnić, dlaczego wskazane
ograniczenie nie rozwiązuje problemu.

Nie ma obowiązku dojścia do zgody. Nowa uwaga lub kontrargument otwiera sprawę
do rozpatrzenia. Ponowne otwarcie uzgodnionej pozycji wymaga nowego uzasadnienia;
jej wcześniejsze wersje i stanowiska pozostają w historii.

Jeżeli recenzenci uzgodnią, że B-2 jest duplikatem A-1, B-2 pozostaje
w historii i raporcie jako pozycja połączona z A-1, z odnośnikiem i uzasadnieniem.
Nie jest usuwana ani przedstawiana jako merytorycznie obalona.

### 10.6 Mechaniczny warunek konsensusu

Rejestr przechowuje wersje treści uwag, proponowanych poprawek i rozstrzygnięć
oraz jawne akceptacje każdego recenzenta. Wersjonowane są ustalenia debaty,
nie dokument źródłowy, którego wersjonowanie pozostaje poza zakresem.

Konsensus zachodzi wyłącznie wtedy, gdy:

- każda uwaga ma rozstrzygnięcie zaakceptowane przez obu recenzentów;
- obie akceptacje dotyczą dokładnie tej samej wersji treści uwagi,
  poprawki (jeżeli dotyczy), rozstrzygnięcia i jego uzasadnienia;
- nie pozostały nierozpatrzone zgłoszenia, propozycje zmian ani sprzeciwy.

Milczenie, pominięcie pozycji i podobieństwo sformułowań nie oznaczają zgody.
Orkiestrator porównuje identyfikatory wersji i deklaracje stanowisk;
nie ocenia semantycznego podobieństwa wypowiedzi.

Nowa treść poprawki wymaga nowej akceptacji obu stron. Zgoda na poprzednią
wersję nie przechodzi automatycznie na następną. Samo wycofanie zarzutu przez
autora również nie oznacza wspólnego odrzucenia: drugi recenzent może nadal
uważać uwagę za zasadną.

**Pierwsza niezależna recenzja wlicza się do limitu rund.** Przy domyślnym
limicie 5, po piątej ukończonej rundzie system kończy pracę: z konsensusem,
jeśli spełniono powyższe warunki, albo ze statusem „brak konsensusu” i listą
pozycji nierozstrzygniętych. Nie rozpoczyna szóstej rundy.

### 10.7 Sterowanie, trwałość i raport końcowy

Każda runda przebiega według tej samej kolejności:

1. Sprawdzenie limitów obu stron; w razie potrzeby PAUZA zgodnie z F-8–F-12.
2. Udostępnienie obu recenzentom tego samego stanu początkowego rundy.
3. Zebranie i sprawdzenie kompletności jednej wypowiedzi od każdego recenzenta.
4. Trwały zapis obu wypowiedzi i wynikającego z nich stanu rundy w `.md`.
5. Powiadomienie człowieka i ustalony czas na weto; brak reakcji nie blokuje pracy.
6. Sprawdzenie konsensusu i limitu rund; zakończenie albo kolejna runda.

Niekompletna lub ucięta odpowiedź nie jest pełną wypowiedzią. Runda z brakującą
wypowiedzią nie zostaje uznana za ukończoną. Po awarii system wznawia pracę
od ostatniej ukończonej rundy, bez ponownego dopisywania już zapisanych rund.
Historia pozostaje append-only (F-21).

Orkiestrator składa raport końcowy z zarejestrowanych treści, bez dodatkowego
wywołania modelu do syntezy. Raport zawiera uwagi uzgodnione, uwagi wspólnie
odrzucone wraz z powodami oraz pozycje sporne ze stanowiskami obu stron.
Zachowuje też odnośniki dla uzgodnionych duplikatów. Brak konsensusu nie
uruchamia trzeciego arbitra; nierozstrzygnięte kwestie trafiają do człowieka.

Ten wariant nie rozwiązuje otwartego pytania o wiarygodny odczyt limitów
obu CLI przed rundą. Wymaganie takiego odczytu nadal wymaga potwierdzenia
wykonalności, szczególnie dla Claude Code.
