from django.db import models


class Channel(models.TextChoices):
    BRI = "BRI", "BRI"
    BCA = "BCA", "BCA"
    MERCHANT_BCA = "MERCHANT_BCA", "Merchant BCA (QRIS)"
    MANDIRI = "MANDIRI", "Mandiri"
    OTOMAX = "OTOMAX", "OTOMAX"


BANK_CHANNELS = [Channel.BRI, Channel.BCA, Channel.MERCHANT_BCA, Channel.MANDIRI]


class ImportStatus(models.TextChoices):
    PARSED = "PARSED", "Parsed"
    PARTIAL = "PARTIAL", "Sebagian (ada baris dikarantina)"
    FAILED = "FAILED", "Gagal"


class MatchStatus(models.TextChoices):
    UNMATCHED = "UNMATCHED", "Belum cocok"
    MATCHED = "MATCHED", "Cocok (otomatis)"
    MANUAL = "MANUAL", "Cocok (manual)"
    PENDING_SETTLE = "PENDING_SETTLE", "Pending Settle"
    IGNORED = "IGNORED", "Diabaikan"


class ManualTag(models.TextChoices):
    ADMIN = "admin", "Biaya Admin"
    TARIK_TUNAI = "tarik_tunai", "Tarik Tunai"
    SETOR_TUNAI = "setor_tunai", "Setor Tunai"
    REVISI = "revisi", "Revisi"
    LAINNYA = "lainnya", "Lainnya"


class OtomaxCategory(models.TextChoices):
    TOPUP_TARTUN = "TOPUP_TARTUN", "Top-up tarik tunai"
    ADMIN = "ADMIN", "Potongan admin"
    REVERSAL = "REVERSAL", "Reversal"
    STOR_IN = "STOR_IN", "Setoran masuk"
    STOR_OUT = "STOR_OUT", "Ambil setoran"
    DEPOSIT = "DEPOSIT", "Deposit manual"
    PAYMENT = "PAYMENT", "Pembayaran"
    OTHER = "OTHER", "Lain-lain"


class MatchType(models.TextChoices):
    AUTO_EXACT = "AUTO_EXACT", "Otomatis - ref & nominal persis"
    AUTO_CORE = "AUTO_CORE", "Otomatis - token inti & nominal"
    AUTO_FUZZY = "AUTO_FUZZY", "Otomatis - mirip (perlu review)"
    AGGREGATE = "AGGREGATE", "Agregat harian (QRIS)"
    MANUAL = "MANUAL", "Manual"


class DiscrepancyKind(models.TextChoices):
    BANK_ONLY = "BANK_ONLY", "Ada di bank, tidak di OTOMAX"
    OTOMAX_ONLY = "OTOMAX_ONLY", "Ada di OTOMAX, tidak di bank"
    AMOUNT_DIFF = "AMOUNT_DIFF", "Ref cocok, nominal beda"
    DUPLICATE = "DUPLICATE", "Dipasangkan lebih dari sekali"


class DiscrepancyStatus(models.TextChoices):
    OPEN = "OPEN", "Terbuka"
    RESOLVED = "RESOLVED", "Selesai"
    WRITTEN_OFF = "WRITTEN_OFF", "Dihapusbukukan"


class ResolutionType(models.TextChoices):
    LATE_MATCH = "LATE_MATCH", "Cocok di hari berikutnya"
    DATA_FIX = "DATA_FIX", "Koreksi data"
    WRITE_OFF = "WRITE_OFF", "Hapus buku"


class DayStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    IN_REVIEW = "IN_REVIEW", "Review"
    CLOSED = "CLOSED", "Ditutup"
