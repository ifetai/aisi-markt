# AISI Marktstand-App (Streamlit): „Erkennst du, was ein Defekt ist?“

Besuchende scannen einen QR-Code und spielen auf dem eigenen Handy zwei Runden mit Rissbildern:
**Runde 1** relevant oder nicht (9 Bilder), **Runde 2** Rissbreite schätzen (5 von 17 Bildern, über die ganze Spanne verteilt).
Der Grossbildschirm zeigt laufend, wie alle geurteilt haben.

| Ansicht | Adresse |
|---|---|
| Handy (im QR-Code) | `https://<app>.streamlit.app/` |
| Grossbildschirm | `https://<app>.streamlit.app/?view=screen` |
| Moderation | `https://<app>.streamlit.app/?view=admin&key=<admin_key>` |

## Deployen auf Streamlit Community Cloud

1. Diesen Ordner in ein GitHub-Repo pushen.
2. Auf share.streamlit.io: **Create app**, Repo wählen, Main file `app.py`.
3. Unter **Advanced settings → Secrets** den Inhalt von `secrets.toml.example` einfügen und ausfüllen. Ohne Secrets läuft die App trotzdem, mit Moderationsschlüssel `aisi`.
4. Nach dem ersten Deploy die Adresse der App in `app_url` eintragen, damit der QR-Code stimmt.

## Daten

Die Antworten liegen im Speicher der App. Alle Handys und der Bildschirm sehen sofort dieselben Zahlen.
Bei einem Neustart der App sind sie weg, **ausser** Google Sheets ist eingerichtet: Dann werden die Antworten alle paar Sekunden ins Blatt `antworten` geschrieben und beim Start wieder geladen.
Dafür das Sheet mit der `client_email` des Service Accounts teilen (Bearbeiter), wie bei teach-the-machine.

In der Moderation: Bildschirmansicht wählen, Zahlen pro Bild, CSV-Export, Neustart (im Sheet wird das alte Blatt archiviert, nicht gelöscht).

## Bilder austauschen

```
content/runde1/relevant/*.jpg        Urteil des Fachs: relevant
content/runde1/irrelevant/*.jpg      Urteil des Fachs: nicht relevant
content/runde2/<Breite>mm_<Nr>.jpg   Rissbreite im Dateinamen, z.B. 2.6mm_01.jpg
```

## Lokal testen

```
pip install -r requirements.txt
streamlit run app.py
```
