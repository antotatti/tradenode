# TradeNode - paper trading automatico

Ogni sera, dopo la chiusura di Wall Street, GitHub lavora al posto tuo, anche a computer spento:

1. rigioca la seduta appena finita con regole fisse (stesse decisioni che il bot avrebbe preso, usando solo i dati disponibili in quel momento);
2. registra le **operazioni simulate** (soldi finti, dati veri) nel diario;
3. prepara la lista dei titoli per la seduta del giorno dopo e la salva con data e ora: e' la prova che non si e' sbirciato;
4. scrive il rapporto giornaliero e, il venerdi', quello settimanale.

**Non invia nessun ordine a nessun broker. Non servono password ne chiavi.** Non e' consulenza finanziaria.

## Installazione (una volta sola, circa 15 minuti, da computer)

1. Entra su github.com con il tuo account. In alto a destra: **+** > **New repository**. Nome: `tradenode`. Scegli **Public**: cosi' Claude puo' leggere i risultati direttamente da un link (vedi in fondo). Premi **Create repository**.
2. Nella pagina del repository premi **uploading an existing file**. Dallo zip che ti ho dato, trascina dentro **tutto il contenuto**, tranne la cartella `.github` (e' nascosta e a volte non si trascina): al suo posto usa il passo 3. Premi **Commit changes**.
3. Il file di pianificazione: premi **Add file** > **Create new file**. Nel nome scrivi esattamente `.github/workflows/tradenode-sera.yml` (le barre creano le cartelle). Apri il file `workflow/tradenode-sera.yml` dello zip, copia tutto il testo e incollalo. Premi **Commit changes**.
4. **Settings** > **Actions** > **General** > in fondo, **Workflow permissions**: scegli **Read and write permissions** e premi **Save**. Senza questo il programma non puo' salvare i risultati.
5. Primo collaudo: scheda **Actions** > a sinistra **TradeNode sera** > **Run workflow** > **Run workflow**. Dopo 3-8 minuti il cerchio diventa verde.

Cosa aspettarsi al primo avvio: la seduta di oggi risulta "senza lista" (e' normale: la lista va preparata la sera prima) e il programma prepara la lista per domani. **Le operazioni simulate partono dalla sera dopo.**

Se il cerchio diventa rosso: apri il lavoro, copia le ultime righe di errore e incollale nella chat del progetto.

## Dove trovi i risultati

Nella cartella `data/` del repository (anche dall'app GitHub sul telefono):

| Cosa | Dove |
| --- | --- |
| Rapporto di ogni sera | `data/rapporti/giornalieri/AAAA-MM-GG.md` |
| Rapporto della settimana | `data/rapporti/settimanali/AAAA-Wnn.md` |
| Tutte le operazioni simulate | `data/diario/operazioni.csv` |
| Una riga per seduta | `data/diario/giornate.csv` |
| Lista titoli di ogni giorno | `data/liste/AAAA-MM-GG.json` |
| Grafici delle sole operazioni | `data/grafici/` |

## Revisione settimanale (e' qui che la skill "impara")

La skill non impara da sola. Ogni venerdi' sera trovi il rapporto settimanale. Non devi incollare nulla: scrivi nella chat del progetto "fai la revisione" (con il link del repository, se non l'hai gia' dato) e Claude legge i file da solo, raggruppa gli errori, controlla se la Time Machine ci azzecca e ti propone eventuali modifiche alle regole. **Le modifiche le approvi tu**; ogni modifica cambia `versione_regole` in `config/config.json` e resta annotata, cosi' si vede quale versione ha fatto cosa.

## Come funziona il recupero dei giorni saltati

GitHub a volte ritarda o salta le esecuzioni pianificate. Il programma ricorda l'ultima seduta elaborata (`data/stato.json`) e, alla prossima esecuzione, recupera fino a 5 giorni arretrati (Yahoo conserva circa 60 giorni di grafici a 5 minuti). Un giorno e' valutato solo se la sua lista era stata preparata la sera prima; altrimenti e' segnato "senza lista" e non viene rigiocato. Una lista preparata dopo l'apertura e' segnata "in ritardo".

## Le regole (config/config.json)

Capitale simulato 5.000 $, perdita massima 50 $ per operazione e 150 $ al giorno, massimo 3 operazioni al giorno, entrate dalle 09:45 alle 15:30 di New York, uscita forzata alle 15:55, stop sempre obbligatorio (meta' del range di apertura), obiettivo a 2 volte il rischio, niente leva, opzioni o vendite allo scoperto. Costi simulati: 0,02 $ per azione tra entrata e uscita (**ipotesi mia, da rivedere**).

Criteri prima di pensare a soldi veri (fissati prima di guardare i risultati): almeno 100 operazioni e 8 settimane, aspettativa positiva dopo i costi, drawdown massimo entro il 10% del capitale, nessuna violazione delle regole.

## Cosa NON fa ancora (e va detto)

- E' una simulazione a fine giornata, non operativita' in diretta: va bene per capire se le regole funzionano, non per ordini veri.
- Non decide da solo il passaggio a soldi veri e non ha nessuna conferma da chiedere, perche' non esiste nessun ordine vero. La conferma obbligatoria arrivera' con la modalita' "automatico con conferma".
- Il confronto con il semplice "compra e tieni" non e' ancora calcolato.
- I dati arrivano da Yahoo (gratuiti, non ufficiali): possono ritardare o bloccare i server cloud. Lo si scopre solo al primo collaudo. Se succede, si passa ai dati gratuiti del conto demo di Alpaca.
- Il calendario di Wall Street dei moduli copre il 2026 e il 2027.
- Le notizie, i bilanci e le operazioni dei politici (moduli news, analista, pelosi) sono presenti ma non fanno parte del ciclo automatico: richiedono giudizio e si usano in chat.

## Repository pubblico: cosa significa

Scelto per la massima automazione: Claude legge i file da un link, senza collegamenti e senza che tu incolli niente. Cosa vede chiunque: i risultati del paper trading (soldi finti), le regole e il codice. Non ci sono password, chiavi o dati personali: il programma non ne usa.

Un'avvertenza di GitHub: in un repository pubblico le esecuzioni pianificate si fermano da sole dopo 60 giorni senza nessuna attivita' nel repository. I salvataggi giornalieri dei risultati sono commit veri, quindi dovrebbero contare come attivita', ma non e' verificato. Se un giorno i rapporti smettono di arrivare: scheda **Actions** > **TradeNode sera** > **Enable workflow**. Claude, quando legge il repository, controlla anche la data dell'ultimo rapporto.

Puoi renderlo privato quando vuoi in **Settings**: da quel momento Claude non potra' piu' leggerlo da un link e dovrai incollare i rapporti in chat.

## Origine e licenza

I moduli in `tradenode/modules/` vengono da *The Day Trader skill* di Jake Schincariol (licenza MIT, vedi `tradenode/LICENSE-day-trader-skill` e `tradenode/ATTRIBUZIONE.md`), copiati senza modifiche. Il programma `run_day.py`, la configurazione e il workflow sono di TradeNode Lab.
