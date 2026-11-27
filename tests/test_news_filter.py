from datetime import datetime

import news_filter as nf

ET = nf.ET_TZ


def _et(*a):
    return ET.localize(datetime(*a))


def test_orari_future():
    assert nf.mercato_aperto("future", _et(2026, 12, 2, 10, 0))          # mercoledì
    assert not nf.mercato_aperto("future", _et(2026, 12, 2, 17, 30))     # pausa giornaliera
    assert not nf.mercato_aperto("future", _et(2026, 12, 4, 17, 30))     # venerdì sera
    assert not nf.mercato_aperto("future", _et(2026, 12, 5, 12, 0))      # sabato
    assert not nf.mercato_aperto("future", _et(2026, 12, 6, 17, 0))      # domenica prima delle 18
    assert nf.mercato_aperto("future", _et(2026, 12, 6, 18, 30))         # domenica sera
    assert not nf.mercato_aperto("future", _et(2026, 12, 25, 10, 0))     # Natale


def test_orari_usa():
    assert nf.mercato_aperto("usa", _et(2026, 12, 2, 10, 0))
    assert not nf.mercato_aperto("usa", _et(2026, 12, 2, 9, 0))
    assert not nf.mercato_aperto("usa", _et(2026, 12, 2, 16, 0))
    assert not nf.mercato_aperto("usa", _et(2026, 11, 26, 12, 0))        # Thanksgiving
    assert nf.mercato_aperto("usa", _et(2026, 11, 27, 12, 0))            # mezza seduta, prima delle 13
    assert not nf.mercato_aperto("usa", _et(2026, 11, 27, 13, 30))       # dopo la chiusura anticipata


def test_minuti_di_mercato():
    # da venerdì 15:00 ET a lunedì 11:00 ET: venerdì 15:00–16:00 + lunedì 09:30–11:00 = 150 minuti
    assert nf.minuti_di_mercato("usa", _et(2026, 12, 4, 15, 0), _et(2026, 12, 7, 11, 0)) == 150
