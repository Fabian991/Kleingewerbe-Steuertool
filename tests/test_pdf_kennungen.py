"""Was auf dem Papier steht: die drei Kennungen des § 34a Satz 1 Nr. 2 UStDV
und der Laendername in der Anschrift.

Gemessen wird am fertigen PDF, nicht an der Vorlage.
"""


import pytest

from app import db
from app.logik import festschreiben as f

pdfplumber = pytest.importorskip("pdfplumber")


def _text(pfad: str) -> str:
    with pdfplumber.open(pfad) as pdf:
        return "\n".join((seite.extract_text() or "") for seite in pdf.pages)


@pytest.fixture
def eingerichtet(conn, betrieb, kunde_id):
    conn.execute(
        "UPDATE betrieb SET beginn_am = '2020-01-01', iban = 'DE02120300000000202051'"
        " WHERE id = 1"
    )
    conn.execute("UPDATE kunde SET satz_cent = 8000 WHERE id = ?", (kunde_id,))
    return kunde_id


def _zeit(conn, kunde_id):
    cur = conn.execute(
        "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
        " VALUES (?, 'Beratung', '2026-03-02', 60, ?)",
        (kunde_id, db.jetzt_utc()),
    )
    return [int(cur.lastrowid)]


def test_kleinunternehmer_idnr_steht_auf_der_rechnung(conn, eingerichtet, tmp_path):
    """Die dritte Kennung. Wer nur sie hat, bekam eine Rechnung OHNE jede
    Kennung -- die Pruefung liess sie durch, das Papier zeigte sie nicht."""
    conn.execute(
        "UPDATE betrieb SET steuernummer = NULL, ust_idnr = NULL,"
        " kleinunternehmer_idnr = 'DE123456789-EX' WHERE id = 1"
    )
    rid = f.entwurf_anlegen(conn, eingerichtet, _zeit(conn, eingerichtet))
    ergebnis = f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
    assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler
    text = _text(ergebnis.pdf_pfad)
    assert "Kleinunternehmer-IdNr." in text
    assert "DE123456789-EX" in text


def test_laendername_statt_kuerzel_in_der_anschrift(conn, eingerichtet, tmp_path):
    """"AT" als letzte Zeile liest ein Empfaenger in Salzburg als Tippfehler;
    DIN 5008 will den Laendernamen."""
    conn.execute(
        "UPDATE kunde SET land = 'AT', plz = '5020', ort = 'Salzburg',"
        " leistungsort = 'EU_B2B_REVERSE_CHARGE', ust_idnr = 'ATU12345678' WHERE id = ?",
        (eingerichtet,),
    )
    conn.execute("UPDATE betrieb SET ust_idnr = 'DE999999999' WHERE id = 1")
    rid = f.entwurf_anlegen(conn, eingerichtet, _zeit(conn, eingerichtet))
    ergebnis = f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
    assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler
    text = _text(ergebnis.pdf_pfad)
    assert "ÖSTERREICH" in text
    assert "\nAT\n" not in text


# ------------------------------------------- genau EINE Kennung, nur einmal


def _fest(conn, kunde_id, tmp_path):
    rid = f.entwurf_anlegen(conn, kunde_id, _zeit(conn, kunde_id))
    ergebnis = f.festschreiben(conn, rid, belegordner=tmp_path / "belege",
                               bestaetigte_warnungen=["G22", "G25"])
    assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler
    return rid, ergebnis


def test_mit_ust_idnr_bleibt_die_steuernummer_weg(conn, eingerichtet, tmp_path):
    """§ 34a Satz 1 Nr. 2 UStDV verlangt EINE Kennung. Steht die USt-IdNr.
    drauf, bleibt die Steuernummer -- der Schluessel zum Steuerkonto -- von
    der Rechnung fern. Vorher stand sie zweimal da."""
    conn.execute("UPDATE betrieb SET ust_idnr = 'DE999999999' WHERE id = 1")
    _, ergebnis = _fest(conn, eingerichtet, tmp_path)
    text = _text(ergebnis.pdf_pfad)
    assert text.count("DE999999999") == 1
    assert "12/345/67890" not in text
    assert "Steuernummer" not in text


def test_nur_steuernummer_steht_genau_einmal(conn, eingerichtet, tmp_path):
    _, ergebnis = _fest(conn, eingerichtet, tmp_path)
    text = _text(ergebnis.pdf_pfad)
    assert text.count("12/345/67890") == 1
    assert text.count("Steuernummer") == 1


def test_kleinunternehmer_idnr_vor_steuernummer(conn, eingerichtet, tmp_path):
    conn.execute("UPDATE betrieb SET kleinunternehmer_idnr = 'DE123456789-EX' WHERE id = 1")
    _, ergebnis = _fest(conn, eingerichtet, tmp_path)
    text = _text(ergebnis.pdf_pfad)
    assert text.count("DE123456789-EX") == 1
    assert "12/345/67890" not in text


def _oesterreich(conn, kunde_id):
    conn.execute(
        "UPDATE kunde SET land = 'AT', plz = '5020', ort = 'Salzburg',"
        " leistungsort = 'EU_B2B_REVERSE_CHARGE', ust_idnr = 'ATU12345678' WHERE id = ?",
        (kunde_id,),
    )


def test_reverse_charge_druckt_die_steuernummer_statt_der_ku_idnr(
    conn, eingerichtet, tmp_path
):
    """Fuer § 14 Abs. 4 Satz 1 Nr. 2 UStG genuegt die Kleinunternehmer-IdNr.
    nicht. Hat der Betrieb beide, steht auf der Oesterreich-Rechnung die
    Steuernummer, und die USt-IdNr. des Empfaengers steht dabei."""
    _oesterreich(conn, eingerichtet)
    conn.execute("UPDATE betrieb SET kleinunternehmer_idnr = 'DE123456789-EX' WHERE id = 1")
    _, ergebnis = _fest(conn, eingerichtet, tmp_path)
    text = _text(ergebnis.pdf_pfad)
    assert text.count("12/345/67890") == 1
    assert "DE123456789-EX" not in text
    assert "ATU12345678" in text


def test_reverse_charge_mit_eigener_ust_idnr(conn, eingerichtet, tmp_path):
    """Beide USt-IdNrn. stehen drauf: die eigene und die des Empfaengers."""
    _oesterreich(conn, eingerichtet)
    conn.execute("UPDATE betrieb SET ust_idnr = 'DE999999999' WHERE id = 1")
    _, ergebnis = _fest(conn, eingerichtet, tmp_path)
    text = _text(ergebnis.pdf_pfad)
    assert text.count("DE999999999") == 1
    assert "ATU12345678" in text
    assert "12/345/67890" not in text


# -------------------------------------- alte Rechnungen: der Schnappschuss


def test_nachgezogenes_pdf_druckt_den_schnappschuss(conn, eingerichtet, tmp_path):
    """Das einzige, was ein gespeichertes PDF neu erzeugt, ist pdf_nachziehen
    -- und das nur, wenn die Datei fehlt. Dann aus dem Absender-Schnappschuss:
    Name, Kennung und Bankverbindung von damals, nicht von heute."""
    from pathlib import Path

    rid, ergebnis = _fest(conn, eingerichtet, tmp_path)
    conn.execute(
        "UPDATE betrieb SET name = 'Neuer Name', ust_idnr = 'DE888888888',"
        " iban = 'DE89370400440532013000', inhaber = 'Jemand Anders' WHERE id = 1"
    )
    Path(ergebnis.pdf_pfad).unlink()
    nachgezogen = f.pdf_nachziehen(conn, rid, tmp_path / "belege")
    assert nachgezogen.pdf_fehler is None, nachgezogen.pdf_fehler
    text = _text(nachgezogen.pdf_pfad)
    assert "Mein Kleingewerbe" in text and "Neuer Name" not in text
    assert "12/345/67890" in text and "DE888888888" not in text
    assert "DE02 1203 0000 0000 2020 51" in text and "Jemand Anders" not in text


def test_vorhandenes_pdf_bleibt_bytegleich(conn, eingerichtet, tmp_path):
    from pathlib import Path

    rid, ergebnis = _fest(conn, eingerichtet, tmp_path)
    vorher = Path(ergebnis.pdf_pfad).read_bytes()
    conn.execute("UPDATE betrieb SET name = 'Neuer Name' WHERE id = 1")
    with pytest.raises(f.FestschreibenFehler, match="vorhanden"):
        f.pdf_nachziehen(conn, rid, tmp_path / "belege")
    with pytest.raises(f.FestschreibenFehler, match="Entwurf"):
        f.vorschau_daten(conn, rid)
    assert Path(ergebnis.pdf_pfad).read_bytes() == vorher


# ------------------------------------------- Pruefung R02 bei Reverse Charge


def _codes(conn, kunde_id) -> dict[str, str]:
    from app.logik import validierung

    rid = f.entwurf_anlegen(conn, kunde_id, _zeit(conn, kunde_id))
    return {b.code: b.stufe for b in validierung.pruefe(conn, rid)}


def test_reverse_charge_nur_mit_ku_idnr_ist_rot(conn, eingerichtet):
    """§ 14 Abs. 4 Satz 1 Nr. 2 UStG kennt nur Steuernummer oder USt-IdNr.;
    die Kleinunternehmer-IdNr. genuegt fuer Reverse Charge nicht."""
    _oesterreich(conn, eingerichtet)
    conn.execute(
        "UPDATE betrieb SET steuernummer = NULL, kleinunternehmer_idnr = 'DE123456789-EX'"
        " WHERE id = 1"
    )
    assert _codes(conn, eingerichtet).get("R02") == "ROT"


def test_reverse_charge_mit_steuernummer_genuegt(conn, eingerichtet):
    """Rechtlich genuegt die Steuernummer -- G22 empfiehlt die USt-IdNr."""
    _oesterreich(conn, eingerichtet)
    codes = _codes(conn, eingerichtet)
    assert "R02" not in codes
    assert codes.get("G22") == "GELB"


def test_inland_nur_mit_ku_idnr_genuegt(conn, eingerichtet):
    conn.execute(
        "UPDATE betrieb SET steuernummer = NULL, kleinunternehmer_idnr = 'DE123456789-EX'"
        " WHERE id = 1"
    )
    assert "R02" not in _codes(conn, eingerichtet)
