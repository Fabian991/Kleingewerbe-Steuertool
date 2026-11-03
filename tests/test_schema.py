"""Das Schema selbst muss die fachlichen Fehler verhindern, nicht nur die Anwendung.

Wer am Tool vorbei direkt in die SQLite-Datei schreibt, muss auf die gleichen
Sperren laufen -- sonst ist die Unveraenderbarkeit nach § 146 Abs. 4 AO nur
behauptet.
"""

import sqlite3

import pytest

from app import db


def _rechnung(conn, kunde_id, *, status="ENTWURF", brutto=10000, faellig=None,
              nummer=None, seq=None, typ="RECHNUNG", storno_von=None):
    cur = conn.execute(
        """
        INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,
                              zahlungsziel_tage, faellig_am, netto_cent, brutto_cent,
                              status, storno_von_id, festgeschrieben_am, erstellt_am)
        VALUES (?, ?, ?, 2026, ?, '2026-03-01', 14, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            typ, nummer, seq, kunde_id, faellig, brutto, brutto, status, storno_von,
            db.jetzt_utc() if status == "FESTGESCHRIEBEN" else None, db.jetzt_utc(),
        ),
    )
    return cur.lastrowid


def _zeit(conn, kunde_id, **felder):
    spalten = {
        "kunde_id": kunde_id, "titel": "Beratung", "datum": "2026-03-01",
        "dauer_minuten": 60, "erfasst_am": db.jetzt_utc(),
    }
    spalten.update(felder)
    namen = ", ".join(spalten)
    frage = ", ".join("?" * len(spalten))
    cur = conn.execute(
        f"INSERT INTO zeiteintrag ({namen}) VALUES ({frage})", tuple(spalten.values())
    )
    return cur.lastrowid


def _position(conn, rechnung_id, *, pos_nr=1, menge=1000, preis=8000,
              nachlass=0, grund=None, quelle="ZEIT"):
    betrag = (menge * preis + 500) // 1000 - nachlass
    cur = conn.execute(
        """
        INSERT INTO rechnungsposition (rechnung_id, pos_nr, bezeichnung, menge_tausendstel,
               einzelpreis_cent, nachlass_cent, nachlass_grund, betrag_cent, quelle)
        VALUES (?, ?, 'Beratung', ?, ?, ?, ?, ?, ?)
        """,
        (rechnung_id, pos_nr, menge, preis, nachlass, grund, betrag, quelle),
    )
    return cur.lastrowid


class TestPflichtangaben:
    def test_betrieb_ohne_steuerkennung_wird_abgewiesen(self, conn):
        """§ 34a Satz 1 Nr. 2 UStDV: eine der drei Kennungen ist Pflicht."""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO betrieb (id, name, strasse, plz, ort)"
                " VALUES (1, 'Betrieb', 'Weg 1', '20095', 'Hamburg')"
            )

    def test_ust_idnr_genuegt_ebenfalls(self, conn):
        conn.execute(
            "INSERT INTO betrieb (id, name, strasse, plz, ort, ust_idnr)"
            " VALUES (1, 'Betrieb', 'Weg 1', '20095', 'Hamburg', 'DE123456789')"
        )
        assert conn.execute("SELECT COUNT(*) c FROM betrieb").fetchone()["c"] == 1

    def test_standardhinweis_nennt_die_steuerfreiheit(self, conn, betrieb):
        """§ 19 Abs. 1 UStG sagt seit 2025 'steuerfrei', nicht 'wird nicht erhoben'."""
        text = conn.execute("SELECT hinweis_19_text FROM betrieb").fetchone()[0]
        assert "steuerfrei" in text.lower()
        assert "§ 19" in text
        assert "nicht erhoben" not in text.lower()

    def test_umsatzsteuer_kann_nicht_ausgewiesen_werden(self, conn, kunde_id):
        """Kleinunternehmer schulden ausgewiesene USt nach § 14c Abs. 1 UStG."""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO rechnung (kunde_id, steuer_cent, erstellt_am) VALUES (?, 1900, ?)",
                (kunde_id, db.jetzt_utc()),
            )

    def test_festgeschrieben_ohne_nummer_wird_abgewiesen(self, conn, kunde_id):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO rechnung (kunde_id, status, datum, festgeschrieben_am, erstellt_am)"
                " VALUES (?, 'FESTGESCHRIEBEN', '2026-03-01', ?, ?)",
                (kunde_id, db.jetzt_utc(), db.jetzt_utc()),
            )

    def test_storno_braucht_bezug(self, conn, kunde_id):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO rechnung (typ, kunde_id, erstellt_am) VALUES ('STORNO', ?, ?)",
                (kunde_id, db.jetzt_utc()),
            )

    def test_entwurf_kann_nicht_versendet_sein(self, conn, kunde_id):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO rechnung (kunde_id, status, versendet_am, erstellt_am)"
                " VALUES (?, 'ENTWURF', '2026-03-02', ?)",
                (kunde_id, db.jetzt_utc()),
            )


class TestNullrechnung:
    """0-€-Positionen sind erlaubt, 0-€-Rechnungen nicht.

    Eine Rechnung ueber 0,00 € rechnet ueber keinen Umsatz gegen Entgelt ab
    (§ 1 Abs. 1 Nr. 1 UStG), ist damit keine Rechnung im Sinne des § 14 Abs. 1
    UStG und verbrennt eine Nummer fuer ein Nicht-Dokument.
    """

    def test_nullrechnung_wird_gesperrt(self, conn, betrieb, kunde_id):
        with pytest.raises(sqlite3.IntegrityError, match="0,00 EUR"):
            _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1, brutto=0)

    def test_nullrechnung_auch_beim_festschreiben_gesperrt(self, conn, betrieb, kunde_id):
        rid = _rechnung(conn, kunde_id, brutto=0)
        with pytest.raises(sqlite3.IntegrityError, match="0,00 EUR"):
            conn.execute(
                "UPDATE rechnung SET status='FESTGESCHRIEBEN', nummer='A', nummer_seq=1,"
                " festgeschrieben_am=? WHERE id=?",
                (db.jetzt_utc(), rid),
            )

    def test_ohne_betrieb_wird_vorsichtshalber_gesperrt(self, conn, kunde_id):
        """Ohne angelegten Betrieb gilt die Sperre, nicht die Freigabe."""
        with pytest.raises(sqlite3.IntegrityError, match="0,00 EUR"):
            _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1, brutto=0)

    def test_flag_hebt_die_sperre_auf(self, conn, betrieb, kunde_id):
        conn.execute("UPDATE betrieb SET nullrechnung_zulassen = 1 WHERE id = 1")
        _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1, brutto=0)

    def test_leistungsnachweis_ist_der_erlaubte_weg(self, conn, betrieb, kunde_id):
        """Erbrachte Arbeit ausweisen, ohne ein Entgelt abzurechnen."""
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="LN-2026-0001",
                        seq=1, brutto=0, typ="LEISTUNGSNACHWEIS")
        chip = conn.execute("SELECT chip FROM v_rechnung_status WHERE id=?", (rid,)).fetchone()[0]
        assert chip == "LEISTUNGSNACHWEIS"

    def test_leistungsnachweis_mit_betrag_wird_abgewiesen(self, conn, betrieb, kunde_id):
        with pytest.raises(sqlite3.IntegrityError):
            _rechnung(conn, kunde_id, brutto=5000, typ="LEISTUNGSNACHWEIS")

    def test_keine_zahlung_auf_eine_null_rechnung(self, conn, betrieb, kunde_id):
        conn.execute("UPDATE betrieb SET nullrechnung_zulassen = 1 WHERE id = 1")
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1, brutto=0)
        with pytest.raises(sqlite3.IntegrityError, match="festgeschriebenen Rechnung mit Betrag"):
            conn.execute(
                "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, '2026-04-01', 1)",
                (rid,),
            )

    def test_keine_zahlung_auf_einen_entwurf(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, '2026-04-01', 1)",
                (rid,),
            )


class TestPositionen:
    def test_kulanzposition_als_nachlass(self, conn, kunde_id):
        """Die Arbeit ist sichtbar bewertet UND als nicht berechnet ausgewiesen."""
        rid = _rechnung(conn, kunde_id)
        _position(conn, rid, nachlass=8000, grund="Kulanz")
        betrag = conn.execute(
            "SELECT betrag_cent FROM rechnungsposition WHERE rechnung_id=?", (rid,)
        ).fetchone()[0]
        assert betrag == 0

    def test_nachlass_ohne_grund_wird_abgewiesen(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id)
        with pytest.raises(sqlite3.IntegrityError):
            _position(conn, rid, nachlass=8000, grund=None)

    def test_nullpreis_ohne_grund_wird_abgewiesen(self, conn, kunde_id):
        """Sonst steht auf dem PDF eine nackte Null und sieht wie ein Fehler aus."""
        rid = _rechnung(conn, kunde_id)
        with pytest.raises(sqlite3.IntegrityError):
            _position(conn, rid, preis=0, grund=None)

    def test_menge_null_wird_abgewiesen(self, conn, kunde_id):
        """§ 34a Satz 1 Nr. 4 UStDV verlangt Menge bzw. Umfang der Leistung.

        Tritt real auf, wenn die Rundung mit ABWAERTS eine Position auf null bringt.
        """
        rid = _rechnung(conn, kunde_id)
        with pytest.raises(sqlite3.IntegrityError):
            _position(conn, rid, menge=0)

    def test_betragsformel_ist_in_der_datenbank_festgenagelt(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO rechnungsposition (rechnung_id, pos_nr, bezeichnung,
                       menge_tausendstel, einzelpreis_cent, betrag_cent, quelle)
                VALUES (?, 1, 'Beratung', 1000, 8000, 9999, 'ZEIT')
                """,
                (rid,),
            )

    def test_formel_stimmt_mit_der_python_rundung_ueberein(self, conn, kunde_id):
        from app.logik.rundung import betrag_cent

        rid = _rechnung(conn, kunde_id)
        for nr, (menge, preis) in enumerate(
            [(750, 8000), (820, 7333), (1750, 8500), (1, 1), (333, 999)], start=1
        ):
            _position(conn, rid, pos_nr=nr, menge=menge, preis=preis)
            aus_db = conn.execute(
                "SELECT betrag_cent FROM rechnungsposition WHERE rechnung_id=? AND pos_nr=?",
                (rid, nr),
            ).fetchone()[0]
            assert aus_db == betrag_cent(menge, preis), f"{menge} x {preis}"


class TestUnveraenderbarkeit:
    def test_journal_erlaubt_kein_update(self, conn):
        db.protokolliere(conn, "TEST", "rechnung", 1)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE journal SET aktion = 'GEFAELSCHT'")

    def test_journal_erlaubt_kein_delete(self, conn):
        db.protokolliere(conn, "TEST", "rechnung", 1)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM journal")

    def test_festgeschriebene_rechnung_ist_gesperrt(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1)
        with pytest.raises(sqlite3.IntegrityError, match="unveraenderbar"):
            conn.execute("UPDATE rechnung SET brutto_cent = 1 WHERE id = ?", (rid,))

    def test_storno_bleibt_moeglich(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1)
        conn.execute(
            "UPDATE rechnung SET status='STORNIERT', storno_grund='Tippfehler' WHERE id=?", (rid,)
        )
        assert conn.execute(
            "SELECT status FROM rechnung WHERE id=?", (rid,)
        ).fetchone()[0] == "STORNIERT"

    def test_storniert_ist_endzustand(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1)
        conn.execute("UPDATE rechnung SET status='STORNIERT' WHERE id=?", (rid,))
        with pytest.raises(sqlite3.IntegrityError, match="Endzustand"):
            conn.execute("UPDATE rechnung SET status='FESTGESCHRIEBEN' WHERE id=?", (rid,))

    def test_entwurf_wird_geloescht_nicht_storniert(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id)
        with pytest.raises(sqlite3.IntegrityError, match="geloescht, nicht storniert"):
            conn.execute("UPDATE rechnung SET status='STORNIERT' WHERE id=?", (rid,))

    def test_versandvermerk_bleibt_moeglich(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1)
        conn.execute("UPDATE rechnung SET versendet_am=? WHERE id=?", (db.heute(), rid))
        conn.execute("UPDATE rechnung SET pdf_sha256='abc' WHERE id=?", (rid,))

    def test_festgeschriebene_rechnung_kann_nicht_geloescht_werden(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1)
        with pytest.raises(sqlite3.IntegrityError, match="nicht geloescht"):
            conn.execute("DELETE FROM rechnung WHERE id = ?", (rid,))

    def test_entwurf_darf_verworfen_werden(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id)
        conn.execute("DELETE FROM rechnung WHERE id = ?", (rid,))
        assert conn.execute("SELECT COUNT(*) c FROM rechnung").fetchone()["c"] == 0

    def test_nummer_ist_eindeutig(self, conn, kunde_id):
        _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="RE-2026-0001", seq=1)
        with pytest.raises(sqlite3.IntegrityError):
            _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="RE-2026-0001", seq=2)

    def test_abgerechnete_zeit_ist_gesperrt(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1)
        zid = _zeit(conn, kunde_id, rechnung_id=rid)
        with pytest.raises(sqlite3.IntegrityError, match="nur ueber Storno"):
            conn.execute("UPDATE zeiteintrag SET dauer_minuten = 999 WHERE id = ?", (zid,))

    def test_zeit_im_entwurf_bleibt_aenderbar(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id)
        zid = _zeit(conn, kunde_id, rechnung_id=rid)
        conn.execute("UPDATE zeiteintrag SET dauer_minuten = 90 WHERE id = ?", (zid,))


class TestDoppelabrechnungssperre:
    def test_zeiteintrag_kann_nicht_zweimal_abgerechnet_werden(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id)
        zid = _zeit(conn, kunde_id)
        p1 = _position(conn, rid, pos_nr=1)
        p2 = _position(conn, rid, pos_nr=2)
        conn.execute(
            "INSERT INTO position_zeiteintrag (position_id, zeiteintrag_id) VALUES (?, ?)",
            (p1, zid),
        )
        with pytest.raises(sqlite3.IntegrityError, match="bereits auf einer nicht stornierten"):
            conn.execute(
                "INSERT INTO position_zeiteintrag (position_id, zeiteintrag_id) VALUES (?, ?)",
                (p2, zid),
            )

    def test_nach_storno_ist_die_zeit_wieder_abrechenbar(self, conn, kunde_id):
        """Die Luecke, die ein UNIQUE auf zeiteintrag_id erzeugt haette: dort waere
        die Stunde nach einem Storno fuer immer gesperrt -- es sei denn, man
        loescht die Historie 'diese Stunde lag auf RE-2026-0001'."""
        alt = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="RE-2026-0001", seq=1)
        zid = _zeit(conn, kunde_id, rechnung_id=alt)
        p_alt = _position(conn, alt, pos_nr=1)
        conn.execute(
            "INSERT INTO position_zeiteintrag (position_id, zeiteintrag_id) VALUES (?, ?)",
            (p_alt, zid),
        )
        conn.execute("UPDATE rechnung SET status='STORNIERT' WHERE id=?", (alt,))
        conn.execute(
            "UPDATE zeiteintrag SET rechnung_id=NULL,"
            " freigegeben_aus_rechnung_id=? WHERE id=?",
            (alt, zid),
        )

        neu = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="RE-2026-0002", seq=2)
        p_neu = _position(conn, neu, pos_nr=1)
        conn.execute(
            "INSERT INTO position_zeiteintrag (position_id, zeiteintrag_id) VALUES (?, ?)",
            (p_neu, zid),
        )
        # Die Historie bleibt erhalten: der Eintrag lag auf beiden Belegen.
        assert conn.execute(
            "SELECT COUNT(*) c FROM position_zeiteintrag WHERE zeiteintrag_id=?", (zid,)
        ).fetchone()["c"] == 2


class TestTimer:
    def test_nur_ein_timer_laeuft(self, conn, kunde_id):
        _zeit(conn, kunde_id, start_zeit="09:00", end_zeit=None, dauer_minuten=1)
        with pytest.raises(sqlite3.IntegrityError, match="bereits ein Timer"):
            _zeit(conn, kunde_id, start_zeit="10:00", end_zeit=None, dauer_minuten=1)

    def test_nach_dem_stoppen_ist_ein_neuer_timer_moeglich(self, conn, kunde_id):
        zid = _zeit(conn, kunde_id, start_zeit="09:00", end_zeit=None, dauer_minuten=1)
        conn.execute("UPDATE zeiteintrag SET end_zeit='10:00', dauer_minuten=60 WHERE id=?", (zid,))
        _zeit(conn, kunde_id, start_zeit="10:30", end_zeit=None, dauer_minuten=1)

    def test_endzeit_vor_startzeit_wird_abgewiesen(self, conn, kunde_id):
        with pytest.raises(sqlite3.IntegrityError):
            _zeit(conn, kunde_id, start_zeit="12:00", end_zeit="09:00")


class TestAbrechnungsmodus:
    def test_kulanz_braucht_grund_und_satz(self, conn, kunde_id):
        with pytest.raises(sqlite3.IntegrityError):
            _zeit(conn, kunde_id, abrechnungsmodus="KULANZ")
        with pytest.raises(sqlite3.IntegrityError):
            _zeit(conn, kunde_id, abrechnungsmodus="KULANZ", grund="Kulanz")
        _zeit(conn, kunde_id, abrechnungsmodus="KULANZ", grund="Kulanz", satz_cent_snapshot=8000)

    def test_intern_erscheint_nie_auf_einer_rechnung(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id)
        with pytest.raises(sqlite3.IntegrityError):
            _zeit(conn, kunde_id, abrechnungsmodus="INTERN", grund="Akquise", rechnung_id=rid)

    def test_pauschal_braucht_einen_betrag(self, conn, kunde_id):
        with pytest.raises(sqlite3.IntegrityError):
            _zeit(conn, kunde_id, abrechnungsmodus="PAUSCHAL")

    def test_abschreiben_braucht_grund_und_schliesst_abrechnung_aus(self, conn, kunde_id):
        with pytest.raises(sqlite3.IntegrityError):
            _zeit(conn, kunde_id, abgeschrieben_am=db.heute())
        rid = _rechnung(conn, kunde_id)
        with pytest.raises(sqlite3.IntegrityError):
            _zeit(conn, kunde_id, rechnung_id=rid, abgeschrieben_am=db.heute(),
                  abgeschrieben_grund="vergessen")
        _zeit(conn, kunde_id, abgeschrieben_am=db.heute(), abgeschrieben_grund="vergessen")


class TestRechnungStatus:
    def _chip(self, conn, rid):
        return conn.execute("SELECT chip FROM v_rechnung_status WHERE id=?", (rid,)).fetchone()[0]

    def test_entwurf(self, conn, kunde_id):
        assert self._chip(conn, _rechnung(conn, kunde_id)) == "ENTWURF"

    def test_gestellt(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1,
                        faellig="2099-01-01")
        assert self._chip(conn, rid) == "GESTELLT"

    def test_versendet(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1,
                        faellig="2099-01-01")
        conn.execute("UPDATE rechnung SET versendet_am='2026-03-02' WHERE id=?", (rid,))
        assert self._chip(conn, rid) == "VERSENDET"

    def test_ueberfaellig_und_teilbezahlt_sind_gleichzeitig_wahr(self, conn, kunde_id):
        """Genau deshalb sind es zwei Felder und nicht ein Status."""
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1,
                        brutto=10000, faellig="2020-01-01")
        conn.execute(
            "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, '2026-03-10', 4000)",
            (rid,),
        )
        z = conn.execute("SELECT * FROM v_rechnung_status WHERE id=?", (rid,)).fetchone()
        assert z["zahlstatus"] == "TEILBEZAHLT"
        assert z["ueberfaellig"] == 1
        assert z["offen_cent"] == 6000
        assert z["tage_ueber_faellig"] > 0

    def test_bezahlt_ist_nicht_mehr_ueberfaellig(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1,
                        brutto=10000, faellig="2020-01-01")
        conn.execute(
            "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, '2026-03-10', 10000)",
            (rid,),
        )
        z = conn.execute("SELECT * FROM v_rechnung_status WHERE id=?", (rid,)).fetchone()
        assert z["zahlstatus"] == "BEZAHLT"
        assert z["ueberfaellig"] == 0

    def test_storniert(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1)
        conn.execute("UPDATE rechnung SET status='STORNIERT' WHERE id=?", (rid,))
        assert self._chip(conn, rid) == "STORNIERT"


class TestOffenePosten:
    def test_leistungsnachweis_und_storno_stehen_nicht_in_der_pruefliste(
        self, conn, betrieb, kunde_id
    ):
        """Jede der drei Bedingungen verhindert eine Dauerblockade des Abschlusses."""
        offen = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="R1", seq=1)
        _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="LN1", seq=1,
                  brutto=0, typ="LEISTUNGSNACHWEIS")
        # Ein Storno traegt korrekt einen negativen Betrag.
        _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="S1", seq=2,
                  brutto=-10000, typ="STORNO", storno_von=offen)
        ids = [z["id"] for z in conn.execute("SELECT id FROM v_offene_posten")]
        assert ids == [offen]

    def test_bezahlte_rechnung_verlaesst_die_pruefliste(self, conn, betrieb, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="R1", seq=1, brutto=5000)
        conn.execute(
            "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, '2026-04-01', 5000)",
            (rid,),
        )
        assert conn.execute("SELECT COUNT(*) c FROM v_offene_posten").fetchone()["c"] == 0


class TestZeiteintragStatus:
    def _status(self, conn, zid):
        return conn.execute(
            "SELECT status FROM v_zeiteintrag_status WHERE id=?", (zid,)
        ).fetchone()[0]

    def test_aufgeschrieben(self, conn, kunde_id):
        assert self._status(conn, _zeit(conn, kunde_id)) == "AUFGESCHRIEBEN"

    def test_nicht_abrechenbar(self, conn, kunde_id):
        zid = _zeit(conn, kunde_id, abrechnungsmodus="INTERN", grund="Akquise")
        assert self._status(conn, zid) == "NICHT_ABRECHENBAR"

    def test_im_entwurf(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id)
        assert self._status(conn, _zeit(conn, kunde_id, rechnung_id=rid)) == "IM_ENTWURF"

    def test_in_rechnung_gestellt(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1,
                        faellig="2099-01-01")
        assert self._status(conn, _zeit(conn, kunde_id, rechnung_id=rid)) == "IN_RECHNUNG_GESTELLT"

    def test_bezahlt(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1, brutto=8000)
        conn.execute(
            "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, '2026-03-10', 8000)",
            (rid,),
        )
        assert self._status(conn, _zeit(conn, kunde_id, rechnung_id=rid)) == "BEZAHLT"

    def test_kulanz_bleibt_kulanz_auch_auf_bezahlter_rechnung(self, conn, kunde_id):
        """Sonst erschiene verschenkte Arbeit als 'Bezahlt' und fiele aus der
        Verzichtsauswertung heraus."""
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1, brutto=8000)
        conn.execute(
            "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, '2026-03-10', 8000)",
            (rid,),
        )
        zid = _zeit(conn, kunde_id, rechnung_id=rid, abrechnungsmodus="KULANZ",
                    grund="Einarbeitung", satz_cent_snapshot=8000)
        assert self._status(conn, zid) == "OHNE_BERECHNUNG_GESTELLT"

    def test_storno_gibt_den_eintrag_frei(self, conn, kunde_id):
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1)
        zid = _zeit(conn, kunde_id, rechnung_id=rid)
        conn.execute("UPDATE rechnung SET status='STORNIERT' WHERE id=?", (rid,))
        assert self._status(conn, zid) == "AUFGESCHRIEBEN"

    def test_abgeschrieben(self, conn, kunde_id):
        zid = _zeit(conn, kunde_id, abgeschrieben_am=db.heute(), abgeschrieben_grund="vergessen")
        assert self._status(conn, zid) == "ABGESCHRIEBEN"

    def test_wert_kommt_aus_dem_satz_snapshot(self, conn, betrieb, kunde_id):
        """Eine spaetere Satzerhoehung darf den verschenkten Wert nicht veraendern."""
        zid = _zeit(conn, kunde_id, dauer_minuten=60, abrechnungsmodus="KULANZ",
                    grund="Kulanz", satz_cent_snapshot=8000)
        assert conn.execute(
            "SELECT wert_cent FROM v_zeiteintrag_status WHERE id=?", (zid,)
        ).fetchone()[0] == 8000
        conn.execute("UPDATE betrieb SET standardsatz_cent = 12000 WHERE id = 1")
        assert conn.execute(
            "SELECT wert_cent FROM v_zeiteintrag_status WHERE id=?", (zid,)
        ).fetchone()[0] == 8000


class TestVerzicht:
    def test_drei_toepfe_werden_getrennt(self, conn, betrieb, kunde_id):
        """Investition in den eigenen Betrieb, sichtbar verschenkt, unsichtbar
        verschenkt -- die dritte Zahl ist die, die man sehen muss."""
        _zeit(conn, kunde_id, abrechnungsmodus="INTERN", grund="Akquise",
              dauer_minuten=120, satz_cent_snapshot=8000)
        _zeit(conn, kunde_id, abgeschrieben_am=db.heute(), abgeschrieben_grund="vergessen",
              dauer_minuten=60, satz_cent_snapshot=8000)
        rid = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="A", seq=1)
        _zeit(conn, kunde_id, rechnung_id=rid, abrechnungsmodus="KULANZ",
              grund="Einarbeitung", dauer_minuten=30, satz_cent_snapshot=8000)

        nach_art = {
            z["art"]: (z["minuten"], z["wert_cent"])
            for z in conn.execute("SELECT * FROM v_verzicht WHERE jahr='2026'")
        }
        assert nach_art["NICHT_ABRECHENBAR"] == (120, 16000)
        assert nach_art["ABGESCHRIEBEN"] == (60, 8000)
        assert nach_art["OHNE_BERECHNUNG_GESTELLT"] == (30, 4000)


class TestVorzeichen:
    """Positionen liegen positiv in der Datenbank, das Vorzeichen traegt der
    Dokumenttyp. So rechnet die Betragsformel in den Positionen weiter, und auf
    dem Papier steht trotzdem, was der Beleg bewirkt."""

    def test_rechnung_muss_positiv_sein(self, conn, betrieb, kunde_id):
        with pytest.raises(sqlite3.IntegrityError):
            _rechnung(conn, kunde_id, brutto=-5000)

    def test_storno_muss_negativ_sein(self, conn, betrieb, kunde_id):
        offen = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="R1", seq=1)
        with pytest.raises(sqlite3.IntegrityError):
            _rechnung(conn, kunde_id, brutto=5000, typ="STORNO", storno_von=offen)

    def test_storno_ueber_null_wird_abgewiesen(self, conn, betrieb, kunde_id):
        offen = _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="R1", seq=1)
        with pytest.raises(sqlite3.IntegrityError, match="0,00 EUR"):
            _rechnung(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="S1", seq=2,
                      brutto=0, typ="STORNO", storno_von=offen)
