"""Die Pruefregeln einzeln. Hier sitzt die Zahlendreher-Erkennung."""

import pytest

from app import db
from app.logik import festschreiben as fs
from app.logik import validierung as v


@pytest.fixture
def ordner(tmp_path):
    return tmp_path / "belege"


@pytest.fixture
def eingerichtet(conn, betrieb, kunde_id):
    conn.execute("UPDATE betrieb SET beginn_am = '2020-01-01' WHERE id = 1")
    conn.execute(
        "UPDATE kunde SET satz_cent = 8000, angelegt_am = '2020-01-01' WHERE id = ?",
        (kunde_id,),
    )
    return kunde_id


def _zeit(conn, kunde_id, minuten=60, datum="2026-03-02", **rest):
    cur = conn.execute(
        """
        INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, abrechnungsmodus,
                                 grund, satz_cent_snapshot, erfasst_am)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kunde_id, rest.get("titel", "Beratung"), datum, minuten,
            rest.get("modus", "STUNDEN"), rest.get("grund"),
            rest.get("satz_cent_snapshot"), db.jetzt_utc(),
        ),
    )
    return int(cur.lastrowid)


def _entwurf(conn, kunde_id, **rest):
    ids = [_zeit(conn, kunde_id, **rest)]
    return fs.entwurf_anlegen(conn, kunde_id, ids, datum=rest.get("rechnungsdatum", db.heute()))


def _codes(conn, rid) -> set[str]:
    return {b.code for b in v.pruefe(conn, rid)}


def _texte(conn, rid, stufe) -> list[str]:
    return [b.text for b in v.pruefe(conn, rid) if b.stufe == stufe]


class TestSauberIstStill:
    def test_normalbetrieb_erzeugt_keine_warnung(self, conn, eingerichtet):
        """Entscheidend fuer die Wirksamkeit: erscheint im Normalbetrieb eine
        Warnung, wird das Abhaken ein Klickritual und die Pruefung ist
        schlechter als keine."""
        conn.execute(
            "INSERT INTO backup_log (zeitpunkt, pfad, geprueft) VALUES (?, 'x', 1)",
            (db.jetzt_utc(),),
        )
        rid = _entwurf(conn, eingerichtet)
        befunde = v.pruefe(conn, rid)
        assert [b.code for b in befunde if b.stufe != v.BLAU] == []
        assert not v.blockiert(befunde)


class TestRotBlockiert:
    def test_eine_von_drei_kennungen_genuegt(self, conn, eingerichtet):
        """§ 34a Satz 1 Nr. 2 UStDV laesst drei Kennungen zu. Dass gar keine
        gesetzt ist, verhindert schon der CHECK auf betrieb -- siehe
        test_schema.TestPflichtangaben."""
        rid = _entwurf(conn, eingerichtet)
        conn.execute(
            "UPDATE betrieb SET steuernummer = NULL, ust_idnr = 'DE123456789' WHERE id = 1"
        )
        assert "R02" not in _codes(conn, rid)
        conn.execute(
            "UPDATE betrieb SET ust_idnr = NULL, kleinunternehmer_idnr = 'KU-1' WHERE id = 1"
        )
        assert "R02" not in _codes(conn, rid)

    def test_unvollstaendige_kundenanschrift(self, conn, eingerichtet):
        rid = _entwurf(conn, eingerichtet)
        conn.execute("UPDATE kunde SET plz = '' WHERE id = ?", (eingerichtet,))
        assert "R03" in _codes(conn, rid)

    def test_fehlender_paragraf_19_hinweis(self, conn, eingerichtet):
        rid = _entwurf(conn, eingerichtet)
        conn.execute("UPDATE rechnung SET hinweis_19_text = 'Danke' WHERE id = ?", (rid,))
        conn.execute("UPDATE betrieb SET hinweis_19_text = 'Danke' WHERE id = 1")
        assert "R07" in _codes(conn, rid)

    def test_veralteter_hinweistext_wird_gemeldet(self, conn, eingerichtet):
        """§ 19 Abs. 1 UStG spricht seit 2025 von einer Steuerbefreiung."""
        rid = _entwurf(conn, eingerichtet)
        conn.execute(
            "UPDATE rechnung SET hinweis_19_text = "
            "'Gemäß § 19 UStG wird keine Umsatzsteuer erhoben.' WHERE id = ?",
            (rid,),
        )
        assert "G18" in _codes(conn, rid)
        assert "nennt weder" in " ".join(_texte(conn, rid, v.GELB))

    def test_ausgewiesen_formel_wird_auch_gemeldet(self, conn, eingerichtet):
        """Die zweite verbreitete Altformel, und die schwerer zu sehende.

        "Es wird keine Umsatzsteuer ausgewiesen" klingt richtig und ist eine
        Aussage ueber das PAPIER: es sagt, was auf der Rechnung fehlt, nicht
        dass der Umsatz steuerfrei ist. § 34a Satz 1 Nr. 5 UStDV verlangt aber
        den Hinweis auf die geltende Steuerbefreiung.
        """
        rid = _entwurf(conn, eingerichtet)
        conn.execute(
            "UPDATE rechnung SET hinweis_19_text = "
            "'Gemäß § 19 Abs. 1 UStG wird keine Umsatzsteuer ausgewiesen.' WHERE id = ?",
            (rid,),
        )
        assert "G18" in _codes(conn, rid)

    def test_befreiungsformel_ist_in_ordnung(self, conn, eingerichtet):
        """Gegenprobe: die Vorbelegung darf die Regel nicht ausloesen."""
        rid = _entwurf(conn, eingerichtet)
        conn.execute(
            "UPDATE rechnung SET hinweis_19_text = "
            "'Steuerfreie Leistung eines Kleinunternehmers gemäß § 19 Abs. 1 UStG.'"
            " WHERE id = ?",
            (rid,),
        )
        assert "G18" not in _codes(conn, rid)

    def test_formulierung_der_ihk_geht_durch(self, conn, eingerichtet):
        """Ein Wortlaut ist nicht vorgeschrieben — die Befreiung muss erkennbar sein.

        Die IHK Region Stuttgart empfiehlt diesen Satz. Er nennt die Befreiung
        nicht beim Namen, aber die Kleinunternehmerregelung, und das ist nach
        § 34a Satz 1 Nr. 5 UStDV die geforderte Aussage. Eine Regel, die den
        Vorschlag der IHK anmeckert, macht das Abhaken zum Klickritual.
        """
        rid = _entwurf(conn, eingerichtet)
        conn.execute(
            "UPDATE rechnung SET hinweis_19_text = "
            "'Kein Steuerausweis aufgrund der Anwendung der "
            "Kleinunternehmerregelung (§ 19 UStG).' WHERE id = ?",
            (rid,),
        )
        assert "G18" not in _codes(conn, rid)

    def test_kopfsumme_gegen_positionen(self, conn, eingerichtet):
        """Der Klassiker, sobald Nachlaesse ins Spiel kommen."""
        rid = _entwurf(conn, eingerichtet)
        conn.execute("UPDATE rechnung SET brutto_cent = 99999 WHERE id = ?", (rid,))
        assert "R09" in _codes(conn, rid)
        assert "auseinander" in " ".join(_texte(conn, rid, v.ROT))

    def test_nullrechnung(self, conn, eingerichtet):
        """Angepasst: der Text verwies frueher auf einen Leistungsnachweis, den
        keine Seite erzeugen kann -- er nennt jetzt nur die Wege, die es gibt."""
        rid = _entwurf(conn, eingerichtet, modus="KULANZ", grund="Kulanz",
                       satz_cent_snapshot=8000)
        assert "R06" in _codes(conn, rid)
        text = " ".join(_texte(conn, rid, v.ROT))
        assert "abschreiben" in text and "Leistungsnachweis" not in text

    def test_position_ohne_menge(self, conn, eingerichtet):
        rid = _entwurf(conn, eingerichtet)
        # Direkt in die Position schreiben scheitert am CHECK; die Regel greift
        # fuer Daten, die auf anderem Weg entstanden sind.
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE rechnungsposition SET menge_tausendstel = 0 WHERE rechnung_id = ?",
                (rid,),
            )


class TestGelbWarnt:
    def test_zukunftsdatum(self, conn, eingerichtet):
        rid = _entwurf(conn, eingerichtet, rechnungsdatum="2099-01-01")
        assert "G01" in _codes(conn, rid)

    def test_rueckwirkendes_datum(self, conn, eingerichtet):
        rid = _entwurf(conn, eingerichtet, rechnungsdatum="2026-01-01")
        assert "G02" in _codes(conn, rid)

    def test_leistungszeitraum_vor_kundenanlage(self, conn, eingerichtet):
        """Mehr als sechs Monate vor der Kundenanlage: der Abstand war vorher
        beliebig klein, und G05 erschien auf jeder ersten Rechnung mit
        nacherfassten Zeiten -- ein Klickritual, kein Befund."""
        conn.execute("UPDATE kunde SET angelegt_am = '2026-10-01' WHERE id = ?", (eingerichtet,))
        rid = _entwurf(conn, eingerichtet, datum="2026-03-02")
        assert "G05" in _codes(conn, rid)

    def test_leistungszeitraum_vor_betriebsbeginn_hat_eigenen_code(self, conn, eingerichtet):
        """Zwei Befunde mit demselben Code wuerden mit einem Haken quittiert,
        und das Journal haelte nur einen fest."""
        conn.execute("UPDATE betrieb SET beginn_am = '2026-06-01' WHERE id = 1")
        # Die Kundenanlage liegt mehr als sechs Monate nach der Zeit: erst dann
        # meldet G05 (siehe KUNDENANLAGE_TOLERANZ_MONATE).
        conn.execute("UPDATE kunde SET angelegt_am = '2026-10-01' WHERE id = ?", (eingerichtet,))
        codes = _codes(conn, _entwurf(conn, eingerichtet, datum="2026-03-02"))
        assert {"G05", "G21"} <= codes

    def test_zeiten_aus_zwei_jahren(self, conn, eingerichtet):
        ids = [_zeit(conn, eingerichtet, datum="2025-12-28"),
               _zeit(conn, eingerichtet, datum="2026-01-05")]
        rid = fs.entwurf_anlegen(conn, eingerichtet, ids)
        assert "G06" in _codes(conn, rid)

    def test_ausreisser_nach_oben(self, conn, eingerichtet, ordner):
        for _ in range(3):
            rid = _entwurf(conn, eingerichtet)
            fs.festschreiben(conn, rid, belegordner=ordner)
        gross = _entwurf(conn, eingerichtet, minuten=60 * 40)
        assert "G08" in _codes(conn, gross)
        assert "Zahlendreher" in " ".join(_texte(conn, gross, v.GELB))

    def test_abweichender_stundensatz(self, conn, eingerichtet):
        rid = _entwurf(conn, eingerichtet, satz_cent_snapshot=12000)
        assert any(c.startswith("G09") for c in _codes(conn, rid))

    def test_unplausible_stundenmenge(self, conn, eingerichtet):
        rid = _entwurf(conn, eingerichtet, minuten=60 * 20)
        assert "G10" in _codes(conn, rid)

    def test_weitere_offene_zeiten(self, conn, eingerichtet):
        rid = _entwurf(conn, eingerichtet)
        _zeit(conn, eingerichtet, datum="2026-03-09")
        assert "G12" in _codes(conn, rid)
        assert "Mitnehmen" in " ".join(_texte(conn, rid, v.GELB))

    def test_doppelrechnung(self, conn, eingerichtet, ordner):
        erste = _entwurf(conn, eingerichtet)
        fs.festschreiben(conn, erste, belegordner=ordner)
        datum = conn.execute("SELECT datum FROM rechnung WHERE id = ?", (erste,)).fetchone()[0]
        zweite = _entwurf(conn, eingerichtet, rechnungsdatum=datum)
        assert "G13" in _codes(conn, zweite)

    def test_entwurf_hat_keinen_snapshot_und_kein_g16(self, conn, eingerichtet, ordner):
        """Angepasst: der alte Test setzte einen Schnappschuss von Hand, den ein
        Entwurf nie hat (er entsteht erst beim Festschreiben). G16 war damit
        toter Code und ist entfernt; ein Entwurf druckt immer den Stamm."""
        rid = _entwurf(conn, eingerichtet)
        assert conn.execute(
            "SELECT kunde_snapshot_json FROM rechnung WHERE id = ?", (rid,)
        ).fetchone()[0] is None
        conn.execute("UPDATE kunde SET name = 'Umbenannt GmbH' WHERE id = ?", (eingerichtet,))
        assert not any(c.startswith("G16") for c in _codes(conn, rid))

    def test_grenzwert_warnung_haengt_am_umsatz(self, conn, eingerichtet):
        """Die Warnung kommt vor dem Festschreiben, nicht am Jahresende."""
        rid = _entwurf(conn, eingerichtet, minuten=60 * 2000)
        assert "G14" in _codes(conn, rid)
        text = " ".join(_texte(conn, rid, v.GELB))
        assert "Grenze" in text and "steuerpflichtig" in text


class TestBlau:
    def test_fehlendes_backup(self, conn, eingerichtet):
        rid = _entwurf(conn, eingerichtet)
        assert "B03" in _codes(conn, rid)
        assert "kein geprüftes Backup" in " ".join(_texte(conn, rid, v.BLAU))

    def test_blau_blockiert_nicht(self, conn, eingerichtet):
        rid = _entwurf(conn, eingerichtet)
        assert not v.blockiert(v.pruefe(conn, rid))


class TestSortierungUndGruppen:
    def test_rot_steht_vor_gelb_vor_blau(self, conn, eingerichtet):
        conn.execute("UPDATE kunde SET plz = '' WHERE id = ?", (eingerichtet,))
        rid = _entwurf(conn, eingerichtet, rechnungsdatum="2099-01-01")
        stufen = [b.stufe for b in v.pruefe(conn, rid)]
        assert stufen == sorted(stufen, key=lambda s: {v.ROT: 0, v.GELB: 1, v.BLAU: 2}[s])

    def test_nach_stufe_liefert_alle_drei_schluessel(self, conn, eingerichtet):
        gruppen = v.nach_stufe(v.pruefe(conn, _entwurf(conn, eingerichtet)))
        assert set(gruppen) == {v.ROT, v.GELB, v.BLAU}

    def test_codes_sind_eindeutig(self, conn, eingerichtet):
        """Sonst quittiert ein Haken zwei verschiedene Punkte."""
        conn.execute("UPDATE betrieb SET beginn_am = '2026-06-01' WHERE id = 1")
        conn.execute("UPDATE kunde SET angelegt_am = '2026-06-01' WHERE id = ?", (eingerichtet,))
        befunde = v.pruefe(conn, _entwurf(conn, eingerichtet, datum="2026-03-02"))
        codes = [b.code for b in befunde]
        assert len(codes) == len(set(codes)), codes
