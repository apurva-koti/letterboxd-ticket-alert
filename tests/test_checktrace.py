from datetime import date

from checktrace import CheckTrace, DateRead, TheaterShowings


def _trace():
    trace = CheckTrace(query="You Can See Everything", result_count=1)
    c = trace.candidate("you-can-see-everything-2026-247346", label="you-can-see-everything-2026-247346")
    c.keep, c.reason = True, "director 'Nathan Fielder' matches 'Nathan Fielder'"
    c.calendar = [date(2026, 10, 26), date(2026, 11, 11)]
    read = DateRead(day=date(2026, 10, 26), limit=5)
    read.theaters.append(TheaterShowings("AMC Metreon 16", 1.8, open=["21:45", "22:15"], past=["12:30", "20:30"]))
    read.theaters.append(TheaterShowings("AMC Kabuki 8", 2.7, past=["15:15"]))
    c.dates.append(read)
    c.used_date = date(2026, 10, 26)
    c.add_raw("theaterShowtimeGroupings 247346 2026-10-26", 200, '{"hasShowtimes":true}')
    trace.candidate("other", label="other").keep = False
    trace.candidates["other"].reason = "title-only 0.5 < 0.85"
    return trace


def test_format_shows_every_showing_with_its_fandango_status():
    out = _trace().format("HEADER", include_raw=False)
    assert out.startswith("HEADER\n")
    assert "KEEP you-can-see-everything-2026-247346  - director 'Nathan Fielder' matches" in out
    assert "drop other  - title-only 0.5 < 0.85" in out
    assert "calendar: 2 date(s), 10-26 .. 11-11" in out
    assert "10-26 (5 nearest)  <- used" in out
    assert "1.8mi  open 21:45 22:15 | past 12:30 20:30" in out
    assert "2.7mi  past 15:15" in out
    assert "raw responses" not in out


def test_raw_responses_included_when_asked():
    out = _trace().format("HEADER", include_raw=True)
    assert 'theaterShowtimeGroupings 247346 2026-10-26 status=200 body={"hasShowtimes":true}' in out


def test_anomaly_forces_raw_responses_even_without_a_status_change():
    trace = _trace()
    trace.candidates["you-can-see-everything-2026-247346"].anomalies.append("unknown showtime type 'presale'")
    out = trace.format("HEADER", include_raw=False)
    assert "UNEXPECTED: you-can-see-everything-2026-247346: unknown showtime type 'presale'" in out
    assert "raw responses:" in out
