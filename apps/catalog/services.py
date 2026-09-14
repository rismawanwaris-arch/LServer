from __future__ import annotations

from apps.core.normalize import norm_ref

from .models import Reseller, ResellerAlias


def resolve_reseller(raw_name: str | None) -> Reseller | None:
    """Cari Reseller dari nama mentah OTOMAX lewat tabel alias. None kalau tak dikenal."""
    key = norm_ref(raw_name)
    if not key:
        return None
    alias = ResellerAlias.objects.select_related("reseller").filter(alias_norm=key).first()
    return alias.reseller if alias else None


def learn_alias(raw_name: str, reseller: Reseller) -> ResellerAlias:
    """Daftarkan ejaan baru -> reseller (dipakai dari layar review)."""
    return ResellerAlias.objects.update_or_create(
        alias_norm=norm_ref(raw_name),
        defaults={"alias_raw": raw_name.strip(), "reseller": reseller},
    )[0]
