"""E-Mails als Beleg (.eml).

Alle Mails hier sind selbst gebaut (email.message.EmailMessage) und
anonymisiert -- nach dem Vorbild einer Bestellbestaetigung mit Guthaben und
AGB-Anhang und einer Versandmitteilung. Echte Mails gehoeren nicht ins
Repository: sie tragen Namen, Anschrift und Kartendaten.
"""

from __future__ import annotations

import socket
from datetime import date
from email import policy
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import unquote_plus

import pytest

from app import db
from app.logik.beleg import email_leser, pipeline
from app.logik.beleg.ergebnis import BelegFehler
from tests import test_belegordner as bo
from tests import test_fremdwaehrung as fw

FIXTURES = Path(__file__).parent / "fixtures"
PDF_DE = FIXTURES / "lieferantenrechnung_de.pdf"
XRECHNUNG = FIXTURES / "xrechnung_ubl.xml"
HEUTE = date(2026, 10, 24)

# Die Fixtures aus test_belegordner.py (Datenordner, Eingangsordner, Datenbank
# mit Betrieb) und die Anwendung aus test_fremdwaehrung.py.
datenordner = bo.datenordner
postfach = bo.postfach
conn = bo.conn
_dateien_gelten_als_fertig = bo._dateien_gelten_als_fertig
_nichts_nachzuholen = bo._nichts_nachzuholen
klient = fw.klient
datenbank = fw.datenbank


# ============================================================ Bausteine

VON = '"Beispiel Store" <store-noreply@beispiel.example>'

BESTAETIGUNG = """Bestellung bestätigt

Deine Bestellung ist bestätigt. Unter konto.beispiel.example findest du deine
Rechnung mit ausgewiesener Umsatzsteuer.

Bestellnummer: BS.1111-2222-3333

Beispiel-Armband (Silber)

59,99 €

Menge: 1

Bestellt

26. Mai 2026, 11:51

Gastbestellungsnummer

BS.1111-2222-3333

Bestellt bei

Beispiel Commerce Ltd

Musterstraße 1

Dublin 4

Irland

IE1234567X

Versandadresse

Erika Mustermann

Zahlungsmethode

Karte •••• 0000

Zwischensumme

59,99 €

Versand

0,00 €

Geschätzte Steuern

9,58 €

Summe

59,99 €

Beispiel Store-Guthaben

-45,00 €

Ausstehender Gesamtbetrag

14,99 €

Zur Erinnerung: Du hast das Store-Guthaben und eine andere Zahlungsmethode für
diesen Kauf verwendet.
"""

VERSAND = """Deine Bestellung ist versandbereit

Gute Neuigkeiten! Deine Bestellung wird demnächst abgeholt.

Lieferung: 26. Mai ‑ 27. Mai

Beispiel-Uhr (Schwarz)

ID-Nummer:

64181ABCD000XY

99,99 €

Menge: 1

99,99 €

Bestellt

7. Mai 2026, 20:49

Bestellnummer

BS.1111-2222-3333

Bestellt bei

Beispiel Commerce Ltd

Musterstraße 1

Dublin 4

Ireland

IE1234567X

Store-Guthaben erhalten

Angebot: Store-Guthaben

45,00 €

Das Store-Guthaben wird 24 Stunden nach dem Versand deinem Konto hinzugefügt.
"""


def mail(betreff: str = "Deine Bestellung ist bestätigt", text: str | None = BESTAETIGUNG,
         html: str | None = None, *, von: str = VON,
         datum: str = "Tue, 26 May 2026 02:51:47 -0700", anhaenge=(),
         charset: str = "utf-8", cte: str | None = None) -> bytes:
    """Eine Mail wie aus Apple Mail oder Gmail: Kopf, Text und/oder HTML, Anhaenge."""
    nachricht = EmailMessage()
    nachricht["From"] = von
    nachricht["To"] = "kunde@beispiel.example"
    nachricht["Subject"] = betreff
    nachricht["Date"] = datum
    nachricht["Message-ID"] = "<test@beispiel.example>"
    if text is not None:
        nachricht.set_content(text, charset=charset, cte=cte)
        if html is not None:
            nachricht.add_alternative(html, subtype="html")
    elif html is not None:
        nachricht.set_content(html, subtype="html", charset=charset, cte=cte)
    for name, daten, typ in anhaenge:
        haupt, unter = typ.split("/")
        nachricht.add_attachment(daten, maintype=haupt, subtype=unter, filename=name)
    return nachricht.as_bytes(policy=policy.SMTP)


def agb_pdf() -> bytes:
    return bo.pdf_aus_zeilen([
        "Verkaufsbedingungen des Beispiel Store", "1. Geltungsbereich",
        "Diese Bedingungen gelten für alle Käufe im Beispiel Store.",
        "2. Vertragsschluss", "Der Vertrag kommt mit der Versandbestätigung zustande.",
        "3. Widerrufsrecht", "Du kannst den Vertrag binnen 14 Tagen widerrufen."])


def aufnehmen(datenordner, inhalt: bytes, name: str = "bestellung.eml") -> pipeline.Eingang:
    return pipeline.aufnehmen(inhalt, name, datenordner)


def lies(conn, datenordner, inhalt: bytes, name: str = "bestellung.eml") -> pipeline.Lesung:
    return pipeline.lies(conn, aufnehmen(datenordner, inhalt, name), heute=HEUTE)


def werte(lesung: pipeline.Lesung) -> dict[str, object]:
    return {name: fund.wert for name, fund in lesung.vorschlag.felder.items()}


def buche(conn, datenordner, lesung, **mehr) -> pipeline.Buchung:
    bestaetigt = {**werte(lesung), "waehrung": "EUR", **mehr}
    return pipeline.buche(conn, lesung, bestaetigt, datenordner, heute=HEUTE,
                          notiz=bestaetigt.get("notiz"),
                          lieferant_neu=(bestaetigt["lieferant"],
                                         bestaetigt.get("lieferant_ust_idnr")))


def _hochladen(klient, inhalt: bytes, name: str = "bestellung.eml") -> str:
    antwort = klient.post("/belege/hochladen", files={"datei": (name, inhalt, "message/rfc822")},
                          follow_redirects=False)
    assert antwort.status_code == 303, antwort.text
    return antwort.headers["location"].rsplit("/", 1)[1]


# ============================================================ Erkennen


class TestAnnehmen:
    def test_eml_wird_angenommen(self, datenordner):
        """Eine gueltige Mail (From, Date, Subject) kommt in den Eingang."""
        eingang = aufnehmen(datenordner, mail())
        assert eingang.endung == ".eml" and eingang.pfad.is_file()

    def test_keine_mail_wird_abgewiesen(self, datenordner):
        """Eine .eml, die keine Mail ist (kein Kopf), kommt nicht hinein."""
        with pytest.raises(BelegFehler, match="ist kein E-Mail"):
            aufnehmen(datenordner, b"%PDF-1.7 kein Kopf", "falsch.eml")
        ohne_betreff = b"From: a@b.example\r\nDate: Tue, 26 May 2026 10:00:00 +0200\r\n\r\nText"
        with pytest.raises(BelegFehler):
            aufnehmen(datenordner, ohne_betreff, "ohne.eml")

    def test_lange_received_kopfzeilen_davor(self, datenordner):
        """From, Date und Subject stehen oft erst nach vielen Kilobyte
        Received-Zeilen -- auch das ist eine Mail (auch fuer Dateien, die von
        Hand in den Eingang gelegt wurden)."""
        davor = b"".join(b"Received: from relay%d.beispiel.example by mx\r\n" % n
                         for n in range(400))
        eingang = aufnehmen(datenordner, davor + mail())
        assert pipeline.inhalt_passt(eingang.pfad) is True

    @pytest.mark.parametrize(("name", "was"), [("mail.emlx", "Apple Mail"),
                                               ("mail.msg", "Outlook")])
    def test_emlx_und_msg_mit_anleitung(self, datenordner, name, was):
        """.emlx und .msg: eine verstaendliche Meldung, wie man eine .eml bekommt."""
        with pytest.raises(BelegFehler) as fehler:
            aufnehmen(datenordner, b"egal", name)
        assert was in str(fehler.value)
        assert "in den Finder" in str(fehler.value)
        assert "Original herunterladen" in str(fehler.value)


# ============================================================ Auslesen


class TestAuslesen:
    def test_guthaben_bezahlter_betrag_mit_vermerk(self, conn, datenordner):
        """Teilweise mit Store-Guthaben bezahlt: vorgeschlagen sind die 14,99 €,
        die wirklich bezahlt wurden -- nicht die 59,99 € des Belegs. Der
        Vermerk mit beiden Zahlen steht in der Notiz, der Hinweis in der Maske."""
        lesung = lies(conn, datenordner, mail())
        gelesen = werte(lesung)
        assert gelesen["brutto"] == 1499
        assert gelesen["notiz"] == ("Beleg über 59,99 €, davon 45,00 € mit Guthaben/Gutschein "
                                    "bezahlt; gebucht ist nur der tatsächlich bezahlte Betrag "
                                    "von 14,99 €.")
        assert "netto" not in gelesen and "ust" not in gelesen
        hinweis = " ".join(lesung.vorschlag.meldungen)
        assert "der Beleg zeigt 59,99 €, tatsächlich bezahlt wurden 14,99 €" in hinweis
        assert not lesung.vorschlag.felder["brutto"].sicher

    def test_bestaetigung_felder(self, conn, datenordner):
        """Lieferant aus "Bestellt bei", USt-IdNr. aus dem Text, Datum aus
        "Bestellt" (deutscher Monatsname), Nummer aus "Bestellnummer". Alles
        unsicher -- eine Mail wird normalerweise nicht automatisch gebucht."""
        lesung = lies(conn, datenordner, mail())
        gelesen = werte(lesung)
        assert gelesen["lieferant"] == "Beispiel Commerce Ltd"
        assert gelesen["lieferant_ust_idnr"] == "IE1234567X"
        assert gelesen["belegdatum"] == "2026-05-26"
        assert gelesen["belegnummer"] == "BS.1111-2222-3333"
        assert lesung.stufen == ("email",) and lesung.vorschlag.quelle == "email"
        for feld in ("lieferant", "belegdatum", "belegnummer", "brutto"):
            assert not lesung.vorschlag.felder[feld].sicher, feld

    def test_versand_ohne_summe_und_mit_erhaltenem_guthaben(self, conn, datenordner):
        """Versandmitteilung: keine Summenzeile, aber zweimal 99,99 €; das
        ERHALTENE Store-Guthaben (45,00 €, ein Bonus) mindert nichts."""
        lesung = lies(conn, datenordner, mail("Deine Bestellung ist versandbereit", VERSAND,
                                              datum="Sat, 23 May 2026 02:51:19 -0700"))
        gelesen = werte(lesung)
        assert gelesen["brutto"] == 9999
        assert "notiz" not in gelesen
        assert gelesen["belegdatum"] == "2026-05-07"
        assert gelesen["belegnummer"] == "BS.1111-2222-3333"

    @pytest.mark.parametrize(("zeile", "erwartet"), [
        ("Bestellt am 3. März 2026", "2026-03-03"),
        ("Bestelldatum: 1. Dezember 2025", "2025-12-01"),
        ("Bestellt: 7. Mai 2026, 20:49", "2026-05-07"),
        ("Rechnungsdatum 15. Juli 2026", "2026-07-15"),
    ])
    def test_deutsche_monatsnamen(self, conn, datenordner, zeile, erwartet):
        """"3. März 2026", "1. Dezember 2025" ... als Belegdatum."""
        text = f"Danke für deine Bestellung.\n\n{zeile}\n\nSumme: 10,00 €\n"
        lesung = lies(conn, datenordner, mail(text=text))
        assert werte(lesung)["belegdatum"] == erwartet

    def test_ohne_datum_im_text_gilt_der_kopf(self, conn, datenordner):
        """Kein Datum im Text: der Kopf "Date", in deutscher Zeit -- 23:30 Uhr
        UTC am 31.05. ist in Deutschland schon der 01.06."""
        text = "Danke für deinen Einkauf.\n\nSumme: 10,00 €\n"
        lesung = lies(conn, datenordner, mail(text=text, datum="Sun, 31 May 2026 23:30:00 +0000"))
        fund = lesung.vorschlag.felder["belegdatum"]
        assert fund.wert == "2026-06-01"
        assert "Kopfzeile Date" in fund.nachweis

    def test_reine_html_mail(self, conn, datenordner):
        """Nur HTML: in Text umgewandelt (Tabellenzellen nebeneinander), dann
        ausgelesen wie Text."""
        html = ("<html><head><title>x</title><style>td{color:red}</style></head><body>"
                "<h1>Danke für deine Bestellung</h1><table>"
                "<tr><td>Bestellnummer</td><td>BS-4711-0815</td></tr>"
                "<tr><td>Bestellt am</td><td>3. März 2026</td></tr>"
                "<tr><td>Verkauft von</td><td>Muster Handel GmbH</td></tr>"
                "<tr><td>Gesamtbetrag</td><td>23,80&nbsp;&euro;</td></tr>"
                "</table></body></html>")
        lesung = lies(conn, datenordner, mail(text=None, html=html))
        gelesen = werte(lesung)
        assert (gelesen["belegnummer"], gelesen["belegdatum"], gelesen["brutto"],
                gelesen["lieferant"]) == ("BS-4711-0815", "2026-03-03", 2380,
                                          "Muster Handel GmbH")
        assert "HTML in Text umgewandelt" in lesung.vorschlag.meldungen[0]

    def test_multipart_alternative_nimmt_den_textteil(self, conn, datenordner):
        """text/plain und text/html: der Textteil gilt, nicht das HTML."""
        html = "<p>Summe</p><p>999,00 €</p>"
        gelesen = werte(lies(conn, datenordner, mail(html=html)))
        assert gelesen["brutto"] == 1499

    def test_absender_als_lieferant_ohne_bestellt_bei(self, conn, datenordner):
        """Ohne "Bestellt bei": der Name des Absenders."""
        text = "Deine Quittung\n\nBestellt: 3. März 2026\n\nSumme: 12,00 €\n"
        lesung = lies(conn, datenordner, mail(text=text, von='"Muster Café" <q@cafe.example>'))
        assert werte(lesung)["lieferant"] == "Muster Café"
        assert "Absender" in lesung.vorschlag.felder["lieferant"].nachweis


class TestZeichensaetze:
    @pytest.mark.parametrize(("charset", "cte"), [
        ("utf-8", "quoted-printable"), ("utf-8", "base64"), ("utf-8", "8bit"),
        ("iso-8859-1", "quoted-printable"), ("iso-8859-1", "base64"),
    ])
    def test_umlaute_bleiben(self, conn, datenordner, charset, cte):
        """Quoted-printable, Base64, ISO-8859-1 und UTF-8: Umlaute und das
        Euro-freie Betragsformat kommen richtig an."""
        text = ("Bestätigung\n\nBestellt bei\n\nMüller & Söhne GmbH\n\n"
                "Bestellt: 3. März 2026\n\nGesamtbetrag: 1.234,56 EUR\n")
        lesung = lies(conn, datenordner, mail(text=text, charset=charset, cte=cte))
        gelesen = werte(lesung)
        assert gelesen["lieferant"] == "Müller & Söhne GmbH"
        assert gelesen["belegdatum"] == "2026-03-03"
        assert gelesen["brutto"] == 123456

    def test_unbekannter_zeichensatz_wird_trotzdem_gelesen(self, conn, datenordner):
        """Ein unbekannter Zeichensatz bricht nichts ab: gelesen als UTF-8."""
        roh = ("From: a@beispiel.example\r\nTo: b@beispiel.example\r\nSubject: Quittung\r\n"
               "Date: Tue, 03 Mar 2026 10:00:00 +0100\r\nMIME-Version: 1.0\r\n"
               "Content-Type: text/plain; charset=x-unbekannt\r\n"
               "Content-Transfer-Encoding: 8bit\r\n\r\n"
               "Bestellt: 3. März 2026\r\nSumme: 5,00 €\r\n").encode()
        gelesen = werte(lies(conn, datenordner, roh))
        assert gelesen["brutto"] == 500

    def test_betreff_nach_rfc_2047(self):
        """Ein kodierter Betreff ("=?utf-8?q?...?=") erscheint lesbar."""
        roh = (b"From: a@beispiel.example\r\nSubject: =?utf-8?q?Best=C3=A4tigung?=\r\n"
               b"Date: Tue, 03 Mar 2026 10:00:00 +0100\r\n\r\nText\r\n")
        assert email_leser.lies(roh).betreff == "Bestätigung"


class TestKaputtUndRiesig:
    def test_kaputte_struktur_ergibt_handeingabe_statt_absturz(self, conn, datenordner):
        """Multipart ohne Grenze, abgeschnittenes Base64: kein Absturz, die
        Mail bleibt lesbar (so weit es geht) und zur Handeingabe."""
        roh = (b"From: a@beispiel.example\r\nSubject: Kaputt\r\n"
               b"Date: Tue, 03 Mar 2026 10:00:00 +0100\r\nMIME-Version: 1.0\r\n"
               b"Content-Type: multipart/mixed; boundary=\"fehlt\"\r\n\r\n"
               b"--anders\r\nContent-Type: application/pdf\r\n"
               b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjc=%%%\r\n")
        lesung = lies(conn, datenordner, roh)
        assert lesung.anhang is None
        assert lesung.vorschlag.quelle == "email"

    def test_riesiger_text_wird_gekuerzt(self, conn, datenordner):
        """Ein Text weit ueber der Grenze: gelesen und gezeigt werden die
        ersten Zeichen, mit Hinweis."""
        text = "Summe: 10,00 €\n" + ("Füllzeile mit Text. " * 40 + "\n") * 600
        lesung = lies(conn, datenordner, mail(text=text))
        assert any("sehr lang" in m for m in lesung.vorschlag.meldungen)
        gelesen = email_leser.lies(lesung.eingang.pfad.read_bytes())
        assert gelesen.gekuerzt and len(gelesen.text) == email_leser.TEXT_HOECHSTENS

    def test_ueber_der_groessengrenze(self, datenordner, monkeypatch):
        """Groesser als die Grenze je Datei: abgewiesen wie jede Datei."""
        monkeypatch.setattr(pipeline, "MAX_BYTES", 10_000)
        with pytest.raises(BelegFehler, match="größer als"):
            aufnehmen(datenordner, mail(anhaenge=[("gross.pdf", b"%PDF" + b"x" * 20_000,
                                                   "application/pdf")]))


# ============================================================ Anhaenge


class TestAnhaenge:
    def test_agb_anhang_wird_uebergangen(self, conn, datenordner):
        """Sales-Terms-DE.pdf (Verkaufsbedingungen) ist keine Rechnung: der
        Text der Mail ist der Beleg, die Meldung nennt den Anhang."""
        lesung = lies(conn, datenordner, mail(anhaenge=[("Sales-Terms-DE.pdf", agb_pdf(),
                                                         "application/pdf")]))
        assert lesung.anhang is None
        assert werte(lesung)["brutto"] == 1499
        assert any("Übergangen: Sales-Terms-DE.pdf" in m for m in lesung.meldungen)

    def test_bedingungen_am_inhalt_erkannt(self, conn, datenordner):
        """Auch unter neutralem Namen: am Anfang des Textes stehen Bedingungen."""
        lesung = lies(conn, datenordner, mail(anhaenge=[("dokument.pdf", agb_pdf(),
                                                         "application/pdf")]))
        assert lesung.anhang is None
        assert any("dokument.pdf (Bedingungen" in m for m in lesung.meldungen)

    def test_rechnungs_pdf_im_anhang_ist_der_beleg(self, conn, datenordner):
        """Die PDF-Rechnung im Anhang wird gelesen wie ein Upload und gebucht
        (Rolle ORIGINAL); die Mail kommt mit Rolle EMAIL ins Archiv --
        beide unveraendert, mit Pruefsumme, schreibgeschuetzt."""
        roh = mail("Ihre Rechnung", "Anbei Ihre Rechnung.\n",
                   anhaenge=[("AGB.pdf", agb_pdf(), "application/pdf"),
                             ("Rechnung R0012345678.pdf", PDF_DE.read_bytes(),
                              "application/pdf")])
        lesung = lies(conn, datenordner, roh)
        assert lesung.anhang is not None
        assert lesung.anhang.original_name == "Rechnung R0012345678.pdf"
        assert lesung.vorschlag.quelle == "pdf_text"
        assert werte(lesung)["brutto"] == 829
        assert "Übergangen: AGB.pdf" in lesung.meldungen[0]
        buchung = buche(conn, datenordner, lesung)
        dateien = conn.execute("SELECT rolle, original_name, mime, sha256, pfad FROM belegdatei"
                               " WHERE beleg_id = ? ORDER BY id", (buchung.beleg_id,)).fetchall()
        assert [(d["rolle"], d["mime"]) for d in dateien] == [
            ("ORIGINAL", "application/pdf"), ("EMAIL", "message/rfc822")]
        assert dateien[0]["sha256"] == pipeline.pruefsumme(PDF_DE)
        assert dateien[1]["sha256"] == lesung.eingang.sha256
        for d in dateien:
            pfad = datenordner / d["pfad"]
            assert pipeline.pruefsumme(pfad) == d["sha256"]
            assert pfad.stat().st_mode & 0o777 == pipeline.NUR_LESEN
        assert (datenordner / dateien[1]["pfad"]).read_bytes() == roh
        # Im Eingang bleibt nichts zurueck, auch kein versteckter Anhang.
        assert list((datenordner / "eingang").iterdir()) == []
        assert pipeline.pruefe_integritaet(conn, datenordner) == []

    def test_xrechnung_im_anhang(self, conn, datenordner):
        """Eine XRechnung im Anhang: maßgebend ist ihr XML."""
        roh = mail("Rechnung", "Rechnung im Anhang.\n",
                   anhaenge=[("rechnung.xml", XRECHNUNG.read_bytes(), "application/xml")])
        lesung = lies(conn, datenordner, roh)
        assert lesung.anhang is not None and lesung.vorschlag.quelle == "xml"
        assert lesung.stufen[0] == "xml"
        assert werte(lesung)["brutto"]

    def test_zugferd_cii_im_anhang(self, conn, datenordner):
        """Ein CII-XML (ZUGFeRD/Factur-X) im Anhang: ebenso."""
        roh = mail("Rechnung", "Rechnung im Anhang.\n",
                   anhaenge=[("factur-x.xml", bo.cii(), "text/xml")])
        lesung = lies(conn, datenordner, roh)
        assert lesung.vorschlag.quelle == "xml"
        assert werte(lesung)["belegnummer"] == "RE-2026-0815"

    def test_xml_ohne_e_rechnung_wird_uebergangen(self, conn, datenordner):
        """Ein XML, das keine E-Rechnung ist, ist kein Beleg."""
        lesung = lies(conn, datenordner, mail(anhaenge=[("daten.xml", b"<daten>1</daten>",
                                                         "application/xml")]))
        assert lesung.anhang is None
        assert any("daten.xml (XML, aber keine E-Rechnung)" in m for m in lesung.meldungen)

    def test_eingebettetes_logo_ist_keine_rechnung(self, conn, datenordner):
        """Ein eingebettetes Bild (Logo) ist keine Rechnung."""
        nachricht = EmailMessage()
        for kopf, wert in (("From", VON), ("Subject", "Quittung"),
                           ("Date", "Tue, 26 May 2026 02:51:47 -0700")):
            nachricht[kopf] = wert
        nachricht.set_content(BESTAETIGUNG)
        nachricht.add_related(b"\x89PNG\r\n\x1a\n" + b"0" * 40, maintype="image",
                              subtype="png", cid="<logo>", filename="logo.png")
        mailtext = email_leser.lies(nachricht.as_bytes())
        assert [a.art for a in mailtext.anhaenge] == ["bild"]


# ============================================================ Dubletten


class TestDubletten:
    def test_dieselbe_mail_zweimal(self, conn, datenordner):
        """Dieselbe Mail ein zweites Mal: an der Pruefsumme erkannt."""
        buche(conn, datenordner, lies(conn, datenordner, mail()))
        zweite = lies(conn, datenordner, mail())
        assert zweite.dublette_von == 1

    def test_dieselbe_mail_mit_rechnungsanhang_zweimal(self, conn, datenordner):
        """Auch wenn die Mail nur Begleitdatei (EMAIL) eines PDF ist."""
        roh = mail("Rechnung", "Anbei.\n", anhaenge=[("r.pdf", PDF_DE.read_bytes(),
                                                       "application/pdf")])
        buche(conn, datenordner, lies(conn, datenordner, roh))
        assert lies(conn, datenordner, roh).dublette_von == 1

    def test_bestaetigung_und_versand_derselben_bestellung(self, conn, datenordner):
        """Bestaetigung gebucht, die Versandmail derselben Bestellung kommt
        dazu: gleiche Bestellnummer -- als aehnlich erkannt."""
        buche(conn, datenordner, lies(conn, datenordner, mail()))
        versand = lies(conn, datenordner, mail("versandbereit", VERSAND,
                                               datum="Sat, 23 May 2026 02:51:19 -0700"))
        assert [b["id"] for b in versand.aehnliche] == [1]
        with pytest.raises(pipeline.AehnlicherBeleg, match="BS.1111-2222-3333"):
            buche(conn, datenordner, versand)

    def test_mail_und_pdf_derselben_rechnung(self, conn, datenordner):
        """Erst das PDF gebucht, dann die Mail mit demselben PDF: Pruefsumme.
        Ein anderer Export derselben Rechnung als Mailtext: Belegnummer."""
        bo.von_hand_buchen(conn, datenordner, PDF_DE.read_bytes(), "r.pdf",
                           bo.lieferant(conn))
        mit_pdf = lies(conn, datenordner, mail("Rechnung", "Anbei.\n",
                                               anhaenge=[("r.pdf", PDF_DE.read_bytes(),
                                                          "application/pdf")]))
        assert mit_pdf.dublette_von == 1
        text = ("Ihre Rechnung\n\nVerkauft von: Hetzner Online GmbH\n\nUSt-IdNr. DE812871812\n\n"
                "Rechnungsnummer: R0012345678\n\nRechnungsdatum: 14.03.2026\n\n"
                "Gesamtbetrag: 8,29 €\n")
        als_text = lies(conn, datenordner, mail("Rechnung", text))
        assert [b["id"] for b in als_text.aehnliche] == [1]


# ============================================================ Oberflaeche

SKRIPT_HTML = (
    "<html><head><script>fetch('https://tracker.example/x')</script>"
    "<style>body{background:url(https://tracker.example/bg.png)}</style></head><body>"
    "<div style='display:none'>Vorschautext versteckt</div>"
    "<img src='https://tracker.example/pixel.gif' width='1' height='1' alt='pixel'>"
    "<iframe src='https://tracker.example/frame'></iframe>"
    "<p>Bestellung <b>bestätigt</b></p><p><a href='https://tracker.example/klick'>Zur Bestellung"
    "</a></p><table><tr><td>Bestellt am</td><td>3. März 2026</td></tr>"
    "<tr><td>Summe</td><td>19,99 €</td></tr></table>"
    "<p onclick=\"alert(1)\">&lt;script&gt;nur Text&lt;/script&gt;</p></body></html>")


class TestOberflaeche:
    def test_pruefmaske_zeigt_sichere_textansicht_und_bucht(self, klient, datenbank):
        """Hochladen, Pruefmaske mit Textansicht statt HTML, Notiz mit dem
        Guthaben-Vermerk vorbelegt, buchen: gebucht sind 14,99 €."""
        name = _hochladen(klient, mail())
        maske = klient.get(f"/belege/pruefen/{name}")
        assert maske.status_code == 200
        assert f'src="/belege/eingang/{name}"' in maske.text
        assert "aus dem Text der E-Mail" in maske.text
        assert "mit Guthaben/Gutschein bezahlt" in maske.text
        assert 'value="14,99"' in maske.text
        daten = {"belegdatum": "2026-05-26", "brutto_euro": "14,99", "waehrung": "EUR",
                 "lieferant_neu": "Beispiel Commerce Ltd", "lieferant_ust_idnr": "IE1234567X",
                 "belegnummer": "BS.1111-2222-3333", "quote": "100",
                 "notiz": "Beleg über 59,99 €, davon 45,00 € mit Guthaben bezahlt."}
        antwort = klient.post(f"/belege/pruefen/{name}", data=daten, follow_redirects=False)
        assert antwort.status_code == 303, antwort.text
        with datenbank() as verbindung:
            beleg = verbindung.execute("SELECT * FROM beleg").fetchone()
            assert (beleg["betrag_brutto_cent"], beleg["notiz"]) == (1499, daten["notiz"])
            assert verbindung.execute("SELECT quelle FROM extraktion").fetchone()[0] == "email"
        ansicht = klient.get("/belege/1/datei")
        assert ansicht.headers["content-type"].startswith("text/html")
        assert "Ausstehender Gesamtbetrag" in ansicht.text
        original = klient.get("/belege/1/datei?download=1")
        assert original.headers["content-type"].startswith("message/rfc822")
        assert original.content == mail()

    def test_textansicht_ohne_tags_und_ohne_abruf(self, klient, monkeypatch):
        """Mail mit Skript, Zaehlpixel, Stil und iframe: die Textansicht
        enthaelt nichts davon, nichts wird aus dem Netz geholt, und die
        Antwort verbietet jedes Nachladen (Content-Security-Policy)."""
        def kein_netz(*args, **kwargs):
            raise AssertionError("Netzzugriff beim Lesen einer Mail")

        name = _hochladen(klient, mail("Bestätigung", text=None, html=SKRIPT_HTML))
        monkeypatch.setattr(socket, "create_connection", kein_netz)
        monkeypatch.setattr(socket.socket, "connect", kein_netz)
        ansicht = klient.get(f"/belege/eingang/{name}")
        assert ansicht.status_code == 200
        koerper = ansicht.text.split("<body>", 1)[1]
        for verboten in ("<script", "<img", "<iframe", "tracker.example", "onclick",
                         "Vorschautext versteckt"):
            assert verboten not in koerper, verboten
        assert "Bestellung bestätigt" in koerper and "Summe 19,99 €" in koerper
        assert "&lt;script&gt;nur Text&lt;/script&gt;" in koerper
        assert "default-src 'none'" in ansicht.headers["content-security-policy"]
        assert ansicht.headers["x-content-type-options"] == "nosniff"
        maske = klient.get(f"/belege/pruefen/{name}")
        assert "tracker.example" not in maske.text
        assert maske.text.count("<script") == maske.text.count("</script>")

    def test_rechnungsanhang_in_der_maske_und_mail_in_der_liste(self, klient, datenbank):
        """Mit Rechnung im Anhang zeigt die Maske den Anhang; nach dem Buchen
        steht in der Liste ein Verweis auf die E-Mail (sichere Textansicht)."""
        roh = mail("Rechnung", "Anbei Ihre Rechnung.\n",
                   anhaenge=[("rechnung.pdf", PDF_DE.read_bytes(), "application/pdf")])
        name = _hochladen(klient, roh)
        maske = klient.get(f"/belege/pruefen/{name}")
        assert f"/belege/eingang/{name}/anhang/1" in maske.text
        anhang = klient.get(f"/belege/eingang/{name}/anhang/1")
        assert anhang.headers["content-type"] == "application/pdf"
        assert anhang.content == PDF_DE.read_bytes()
        daten = {"belegdatum": "2026-03-14", "brutto_euro": "8,29", "waehrung": "EUR",
                 "lieferant_neu": "Hetzner Online GmbH", "lieferant_ust_idnr": "DE812871812",
                 "quote": "100"}
        assert klient.post(f"/belege/pruefen/{name}", data=daten,
                           follow_redirects=False).status_code == 303
        assert 'href="/belege/1/mail"' in klient.get("/belege").text
        assert klient.get("/belege/1/datei").content == PDF_DE.read_bytes()
        mailansicht = klient.get("/belege/1/mail")
        assert "Anbei Ihre Rechnung." in mailansicht.text
        assert "rechnung.pdf" in mailansicht.text
        # Doppelklick nach dem Buchen: "bereits gebucht", nicht "fehlt".
        nochmal = klient.post(f"/belege/pruefen/{name}", data=daten, follow_redirects=False)
        assert "bereits als Beleg 1 gebucht" in unquote_plus(nochmal.headers["location"])

    def test_wegwerfen_raeumt_die_anhaenge_mit_weg(self, klient, tmp_path):
        """Wegwerfen aus dem Eingang nimmt die versteckt abgelegten Anhaenge mit."""
        roh = mail("Rechnung", "Anbei.\n", anhaenge=[("r.pdf", PDF_DE.read_bytes(),
                                                       "application/pdf")])
        name = _hochladen(klient, roh)
        klient.get(f"/belege/pruefen/{name}")
        eingang = tmp_path / "eingang"
        assert list(eingang.glob(f".{name}.anhang*"))
        klient.post(f"/belege/eingang/{name}/wegwerfen", follow_redirects=False)
        assert [p.name for p in eingang.iterdir()] == []

    def test_hochladen_nennt_eml(self, klient):
        """Das Upload-Formular nimmt .eml und sagt, wie man eine bekommt."""
        seite = klient.get("/belege").text
        assert ".eml" in seite.split('accept="', 1)[1].split('"', 1)[0]
        assert "Original" in seite and "In Dateien sichern" in seite


# ============================================================ Eingangsordner


class TestEingangsordner:
    def test_mail_mit_rechnung_wird_wie_das_pdf_gebucht(self, conn, datenordner, postfach):
        """Eine .eml mit sicher lesbarer Rechnung eines bekannten Lieferanten
        im Anhang: automatisch gebucht wie das PDF selbst, die Mail als
        Begleitdatei."""
        bo.lieferant(conn)
        roh = mail("Ihre Rechnung", "Anbei.\n", anhaenge=[("Rechnung.pdf", PDF_DE.read_bytes(),
                                                          "application/pdf")])
        bo.lege(postfach, "Rechnung.eml", roh)
        [gebucht] = bo.hole(conn, datenordner, postfach).gebucht
        assert gebucht.brutto_cent == 829
        rollen = [z[0] for z in conn.execute(
            "SELECT rolle FROM belegdatei WHERE beleg_id = ? ORDER BY id", (gebucht.beleg_id,))]
        assert rollen == ["ORIGINAL", "EMAIL"]

    def test_mail_ohne_rechnung_wartet(self, conn, datenordner, postfach):
        """Eine Mail ohne Rechnung im Anhang wartet in der Pruefmaske."""
        bo.lege(postfach, "Bestellung.eml", mail())
        ergebnis = bo.hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.originalname == "Bestellung.eml"
        assert wartend.eingangsname.endswith(".eml")
        assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0

    def test_keine_mail_mit_endung_eml(self, conn, datenordner, postfach):
        """Eine .eml, die keine Mail ist, wird nicht aufgenommen."""
        bo.lege(postfach, "falsch.eml", b"kein Kopf, nur Text")
        ergebnis = bo.hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == [] and ergebnis.wartend == []
        [abgewiesen] = ergebnis.abgewiesen
        assert "ist kein E-Mail" in abgewiesen.grund


# ============================================================ Schema


class TestMigration:
    def test_extraktion_kennt_die_quelle_email(self, tmp_path):
        """Version 20: eine Datenbank aus Version 19 bekommt extraktion mit
        der Quelle 'email' im CHECK -- die vorhandenen Zeilen bleiben."""
        pfad = tmp_path / "alt.sqlite"
        alt = db.oeffne(pfad)
        alt.execute("PRAGMA foreign_keys = OFF")
        alt.execute("DROP TABLE extraktion")
        alt.execute(db._tabellendefinition("extraktion").replace("'email', ", ""))
        alt.execute("INSERT INTO beleg (id, belegdatum, waehrung, betrag_brutto_cent,"
                    " betrag_betrieblich_cent, erfasst_am) VALUES (7, '2026-03-03', 'EUR',"
                    " 100, 100, 'x')")
        alt.execute("INSERT INTO extraktion (beleg_id, quelle, zeitpunkt) VALUES (7, 'xml', 'x')")
        with pytest.raises(Exception, match="CHECK"):
            alt.execute("INSERT INTO extraktion (beleg_id, quelle, zeitpunkt)"
                        " VALUES (8, 'email', 'x')")
        alt.execute("DELETE FROM schema_version")
        alt.execute("INSERT INTO schema_version VALUES (19, ?)", (db.jetzt_utc(),))
        alt.commit()
        alt.close()
        migriert = db.oeffne(pfad)
        try:
            # Mindestens 20: spaetere Versionen laufen in derselben Migration mit.
            assert migriert.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] \
                == db.SCHEMA_VERSION >= 20
            assert migriert.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'MIGRATION'"
                                    " AND objekt_id = 20").fetchone()[0] == 1
            assert [tuple(z) for z in migriert.execute(
                "SELECT beleg_id, quelle FROM extraktion")] == [(7, "xml")]
            migriert.execute("INSERT INTO extraktion (beleg_id, quelle, zeitpunkt)"
                             " VALUES (7, 'email', 'x')")
        finally:
            migriert.close()
