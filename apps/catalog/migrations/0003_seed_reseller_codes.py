"""Data migration: isi awal tabel Reseller (kode -> nama) dari daftar yang diberikan user.

Sengaja pakai migration (bukan cuma seed lewat shell) supaya ikut ter-apply otomatis di
ZimaOS saat deploy (compose.zima.yml menjalankan `migrate` di setiap startup) tanpa perlu
akses shell manual ke server produksi. Idempoten (get_or_create) -- aman dijalankan ulang,
dan tidak menimpa baris yang sudah diedit user lewat halaman Kode Reseller (cuma isi kalau
kode itu belum ada)."""

from django.db import migrations

RESELLER_CODES = [
    ("PLC21", "PLC PC5 CIGENDING"),
    ("PLC25", "PLC ALFA5 NAGROG1"),
    ("PLC30", "PLC ALFA6 CINANGKA"),
    ("PLC31", "PLC BK CIJAMBE"),
    ("PLC36", "PLC PC4 OJEG"),
    ("PLC42", "PLC SS"),
    ("PLC51", "PLC SA"),
    ("PLC52", "PLC SINOM"),
    ("PLC54", "PLC PD2"),
    ("PLC65", "PLC ALFA4 PARAKAN"),
    ("PLC68", "PLC DM"),
    ("PLC69", "PLC RK"),
    ("PLC70", "PLC ALFA2 SINJAY2"),
    ("PLC71", "PLC ALFA3 SINJAY1"),
    ("PLC80", "PLC JH2"),
    ("PLC82", "PLC PD1"),
    ("PLC97", "PLC ASBER1"),
    ("PLC101", "PLC ASBER2"),
    ("PLC102", "PLC BK CIPADUNG"),
    ("PLC106", "PLC BK7 NAGROG2"),
    ("PLC108", "PLC CISA"),
    ("PLC111", "PLC BUNISARI"),
    ("PLC112", "PLC ALFA1 PASIR IMPUN"),
    ("PLC114", "PLC ALFA7 CINGISED"),
    ("PLC115", "PLC BAKSAR1"),
    ("PLC116", "PLC BK5 CIGER"),
    ("PLC120", "PLC CIKADUT 1"),
    ("PLC121", "PLC PC3 CJM"),
    ("PLC123", "PLC BK6 PANGARITAN"),
    ("PLC128", "PLC PD3"),
    ("PLC129", "PLC BAKSAR2"),
    ("PLC130", "PLC CUKANG"),
    ("PLC131", "PLC CIPADUNG 2"),
    ("PLC132", "PLC CL 1"),
    ("PLC133", "PLC CIKADUT 2"),
    ("PLC134", "PLC CIPAGALO CELL"),
    ("PLC135", "PLC CL 2"),
    ("PLC136", "PLC CL 3"),
    ("PLC137", "PLC CIPOREAT"),
    ("PLC138", "PLC CIHAURKUKU"),
    ("PLC139", "PLC CL 4"),
]


def seed_resellers(apps, schema_editor):
    Reseller = apps.get_model("catalog", "Reseller")
    for code, name in RESELLER_CODES:
        Reseller.objects.get_or_create(code=code, defaults={"name": name, "active": True})


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0002_exclusionrule_historicalexclusionrule"),
    ]

    operations = [
        migrations.RunPython(seed_resellers, noop_reverse),
    ]
