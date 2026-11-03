"""Ein erfundener WISO-Export „Buchungen“ im exakten Format der echten Datei.

Kopfzeile, Trennzeichen, Dezimalkomma, Datumsform, Konten (SKR03) und die
Art der Zeilen entsprechen dem Export aus WISO Steuer; Namen, Texte und
Beträge sind erfunden. Abgedeckt: Einnahmen von einem Inlandspartner, von
Plattformen im Ausland (zwei Schreibweisen desselben Zahlers), ein
Geräteverkauf, Ausgaben auf den vorbelegten Konten, ein Software-Titel unter
GWG, Reisekosten, eine Erstattung (Aufwandskonto im Haben), eine
Privatentnahme, eine Anschaffung mit AfA-Zeile, die AfA eines Vorjahresguts
und ein Konto ohne Vorbelegung.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.logik import wiso

#: Die Kopfzeile, wie WISO sie schreibt.
KOPF = ";".join(wiso.KOPF)

ERLOES = ("8195", "Erlöse als Kleinunternehmer nach § 19 Abs. 1 UStG")
BANK = ("1200", "Bank")
AFA = ("4830", "Abschreibungen auf Sachanlagen (ohne AfA auf Fahrzeuge und Gebäude)")


@dataclass(frozen=True)
class Buchung:
    nummer: int
    datum: str
    text: str
    konto: tuple[str, str]
    soll: str = "0,00"
    haben: str = "0,00"
    gegenkonto: tuple[str, str] = BANK
    belegnummer: str = ""
    teil: int = 1
    erfasst: str = "01.07.2026"

    def zeile(self) -> str:
        return ";".join([
            str(self.nummer), str(self.teil), self.datum, self.datum, self.text,
            self.konto[0], self.konto[1], self.gegenkonto[0], self.gegenkonto[1],
            self.soll, self.haben, "", "0,00", "", self.belegnummer, self.erfasst, "", "0",
        ])


BUCHUNGEN: tuple[Buchung, ...] = (
    Buchung(1, "05.01.2026", "Beispiel Office Abo", ("4900", "Sonstige betriebliche Aufwendungen"),
            soll="12,99"),
    Buchung(2, "12.01.2026", "Mustermesse 2026", ("4945", "Fortbildungskosten"), soll="129,00"),
    Buchung(3, "10.01.2026", "Beispiel Partnerprogramm", ERLOES, haben="250,00"),
    Buchung(4, "15.01.2026", "Amazon Affiliate", ERLOES, haben="120,50"),
    Buchung(5, "20.01.2026", "Youtube Ads", ERLOES, haben="80,00"),
    Buchung(6, "22.01.2026", "YouTube Ads", ERLOES, haben="95,25"),
    Buchung(7, "25.01.2026", "Muster Ads", ("4600", "Werbekosten"), soll="50,00"),
    Buchung(8, "27.03.2026", "Beispiel Newsletter", ("4980", "Sonstiger Betriebsbedarf"),
            haben="38,40"),
    Buchung(9, "30.03.2026", "Einkauf privat", ("1800", "Privatentnahmen allgemein"),
            soll="100,00"),
    Buchung(10, "15.04.2026", "Plotter Beispiel [Anl.-Nr.: 1]", ("470", "Computeranlagen"),
            soll="1200,00", belegnummer="4711", erfasst="02.07.2026"),
    Buchung(11, "31.12.2026", "AfA: Plotter Beispiel [Anl.-Nr.: 1]", ("470", "Computeranlagen"),
            haben="300,00", gegenkonto=AFA, erfasst="02.07.2026"),
    Buchung(12, "31.12.2026", "AfA: Kamera Beispiel [Anl.-Nr.: 2]",
            ("490", "Sonstige Betriebs- und Geschäftsausstattung"), haben="400,00",
            gegenkonto=AFA, erfasst="02.07.2026"),
    Buchung(13, "12.05.2026", "Beispiel Software Lizenz",
            ("4855", "Sofortabschreibung geringwertiger Wirtschaftsgüter"), soll="199,00"),
    Buchung(14, "20.05.2026", "Rad Beispiel - Verkauf", ERLOES, haben="280,00"),
    Buchung(15, "21.05.2026", "Fahrt nach Musterstadt zur Messe",
            ("4673", "Reisekosten Unternehmer Fahrtkosten"), soll="60,00"),
    Buchung(16, "22.05.2026", "Musterstadt Verpflegung",
            ("4674", "Reisekosten Unternehmer Verpflegungsmehraufwand"), soll="14,00"),
    Buchung(17, "01.06.2026", "Werkstatt Beispiel", ("4500", "Fahrzeugkosten"), soll="45,00"),
)

#: Summen der erfundenen Datei in Cent -- wie die WISO-Kontensummen.
EINNAHMEN_CENT = 25000 + 12050 + 8000 + 9525 + 28000
AUSGABEN_BEKANNT_CENT = 1299 + 12900 + 5000 + 19900 + 6000 + 1400
AUSGABEN_UNBEKANNT_CENT = 4500
ERSTATTUNG_CENT = 3840
PRIVAT_CENT = 10000
AFA_CENT = 30000 + 40000
ANLAGE_CENT = 120000


def text(buchungen: tuple[Buchung, ...] | list[Buchung] = BUCHUNGEN) -> str:
    """Die Datei als Text, mit Byte-Order-Mark wie das Original."""
    return "﻿" + "\r\n".join([KOPF, *(b.zeile() for b in buchungen)]) + "\r\n"


def datei(buchungen: tuple[Buchung, ...] | list[Buchung] = BUCHUNGEN) -> bytes:
    return text(buchungen).encode("utf-8")


def geaendert(nummer: int, **felder) -> list[Buchung]:
    """Die Beispieldatei mit einer geaenderten Buchung."""
    return [replace(b, **felder) if b.nummer == nummer else b for b in BUCHUNGEN]
