set projectDir to "/Users/matteoperico/Side projects/portfolio-tracker"
set pythonPath to "/Users/matteoperico/anaconda3/bin/python"
set logFile to "/tmp/portfolio-tracker.log"

set pid to do shell script "lsof -ti:5001 2>/dev/null | head -1"

if pid is "" then
	-- Avvia il server
	do shell script pythonPath & " " & quoted form of (projectDir & "/app.py") & " >> " & logFile & " 2>&1 &"
	delay 2
	set newPid to do shell script "lsof -ti:5001 2>/dev/null | head -1"
	if newPid is not "" then
		open location "http://localhost:5001"
		display notification "Aperto su http://localhost:5001" with title "Portfolio Tracker avviato" subtitle "Clicca di nuovo per spegnerlo"
	else
		display dialog "Errore nell'avvio del server." & return & return & "Log: " & logFile buttons {"OK"} default button "OK" with icon stop
	end if
else
	set answer to button returned of (display dialog "Portfolio Tracker è in esecuzione su http://localhost:5001" buttons {"Apri nel browser", "Spegni", "Annulla"} default button "Apri nel browser" with icon note)
	if answer is "Spegni" then
		do shell script "lsof -ti:5001 | xargs kill -9 2>/dev/null; true"
		display notification "Server fermato" with title "Portfolio Tracker spento"
	else if answer is "Apri nel browser" then
		open location "http://localhost:5001"
	end if
end if
