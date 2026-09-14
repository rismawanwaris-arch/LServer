from decimal import Decimal

from apps.core.enums import Channel
from apps.ingest.parsers import parse_file


def test_parse_bri_csv():
    bri_csv = """TGL_TRAN,MUTASI_DEBET,MUTASI_KREDIT,GLSIGN,REMARK_CUSTOM,DESK_TRAN
2026-09-05 21:40:31,0,"1,600,000.00",Cr,DANA20260905034895588601ASEPKURNIAWA,TRANSFER DARI DANA
2026-09-05 22:10:00,"2,500.00",0,Db,BIAYA ADM BULANAN,ADM
"""
    res = parse_file(Channel.BRI, bri_csv)
    assert len(res.bank_rows) == 2
    assert res.bank_rows[0].amount == Decimal("1600000.00")
    assert "DANA20260905034895588601" in res.bank_rows[0].description_raw
    assert res.bank_rows[1].amount == Decimal("-2500.00")


def test_parse_bri_csv_with_trremk():
    bri_csv = (
        '"ID","NOREK","TGL_TRAN","MUTASI_DEBET","MUTASI_KREDIT","GLSIGN","TRREMK","REMARK_CUSTOM"\n'
        '"1","215401000596563","2026-09-12 06:14:36","0.00","200000.00","Cr",'
        '"BFST215401000596563CECEP SUPRIA:SSPIIDJA","Transfer BI-Fast - Cecep"\n'
    )
    res = parse_file(Channel.BRI, bri_csv)
    assert len(res.bank_rows) == 1
    assert res.bank_rows[0].description_raw == "BFST215401000596563CECEP SUPRIA:SSPIIDJA"
    assert res.bank_rows[0].amount == Decimal("200000.00")


def test_parse_mandiri_csv():
    mandiri_csv = """Date;Remark;Reference No.;Debit Amount;Credit Amount;Balance
05/09/26 14.30;TARTUN TF MANDIRI MCM;REF12345;0;6500.00.00;1000000.00
05/09/26 15.00;BIAYA ADM;-;5000.00;0;995000.00
"""
    res = parse_file(Channel.MANDIRI, mandiri_csv)
    assert len(res.bank_rows) == 2
    assert res.bank_rows[0].amount == Decimal("6500.00")
    assert res.bank_rows[1].amount == Decimal("-5000.00")


def test_parse_bca_csv():
    bca_csv = """Rekening : 1234567890
Nama     : KONTER PULSA
Periode  : 05/09/2026 - 05/09/2026
Mata Uang: IDR
Tanggal Transaksi,Keterangan,Cabang,Jumlah,Saldo
05/09/2026,"TRSF E-BANKING CR 0509/FTSCY/WS95011 550,000.00",0000,"550,000.00 CR","10,550,000.00"
05/09/2026,"TARIKAN ATM 100,000.00",0000,"100,000.00 DB","10,450,000.00"
"""
    res = parse_file(Channel.BCA, bca_csv)
    assert len(res.bank_rows) == 2
    assert res.bank_rows[0].amount == Decimal("550000.00")
    assert res.bank_rows[1].amount == Decimal("-100000.00")

    # Also verify bytes input (e.g. from upload_file.read())
    res_bytes = parse_file(Channel.BCA, bca_csv.encode("utf-8"))
    assert len(res_bytes.bank_rows) == 2
    assert res_bytes.bank_rows[0].amount == Decimal("550000.00")


def test_parse_merchant_bca_tsv():
    tsv = """Merchant Name\tMerchant ID\tTotal Frequency\tTotal Amount
ALFA 1 CELL\t004767950\t25\tRp4,374,000
BETA CELL\t004767951\t10\tRp1,200,000
Total\t\t35\tRp5,574,000
"""
    res = parse_file(Channel.MERCHANT_BCA, tsv)
    assert len(res.bank_rows) == 2
    assert res.bank_rows[0].amount == Decimal("4374000")
    assert res.bank_rows[0].outlet_name == "ALFA 1 CELL"
    assert res.bank_rows[0].external_ref == "004767950"


def test_parse_otomax_tsv():
    tsv = """Tanggal\tNama Reseller\tJumlah\tKeterangan
2026-09-05 21:40:00\tPLC134\t1600000\tTARTUN TF BRI DANA20260905034895588601ASEPKURNIAWA
2026-09-05 22:00:00\tPLC001\t550000\tTARTUN EDC BCA 550000
"""
    res = parse_file(Channel.OTOMAX, tsv)
    assert len(res.otomax_rows) == 2
    assert res.otomax_rows[0].amount == Decimal("1600000")
    assert res.otomax_rows[0].reseller_name_raw == "PLC134"


def test_parse_merchant_bca_xlsx():
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "SUMMARY"
    ws.append(["Merchant Name", "Merchant ID", "Total Frequency", "Total Amount"])
    ws.append(["TOKO SUKSES", "001234567", 15, 2500000])
    ws.append(["TOTAL", "", 15, 2500000])

    buf = io.BytesIO()
    wb.save(buf)
    file_bytes = buf.getvalue()

    res = parse_file(Channel.MERCHANT_BCA, file_bytes)
    assert len(res.bank_rows) == 1
    assert res.bank_rows[0].amount == Decimal("2500000")
    assert res.bank_rows[0].outlet_name == "TOKO SUKSES"
    assert res.bank_rows[0].external_ref == "001234567"


def test_parse_otomax_xlsx():
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Report"
    ws.append(["Tanggal", "Nama Reseller", "Jumlah", "Keterangan"])
    ws.append(["2026-09-05 10:15:00", "PLC999", 750000, "DEPOSIT VIA MANDIRI BFST99999"])

    buf = io.BytesIO()
    wb.save(buf)
    file_bytes = buf.getvalue()

    res = parse_file(Channel.OTOMAX, file_bytes)
    assert len(res.otomax_rows) == 1
    assert res.otomax_rows[0].amount == Decimal("750000")
    assert res.otomax_rows[0].reseller_name_raw == "PLC999"
