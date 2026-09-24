from __future__ import annotations

from apps.core.normalize import norm_ref

from .models import ExclusionRule, Reseller, ResellerAlias


def resolve_reseller(raw_name: str | None, alias_map: dict[str, Reseller] | None = None) -> Reseller | None:
    """Cari Reseller dari nama mentah OTOMAX lewat tabel alias. None kalau tak dikenal."""
    key = norm_ref(raw_name)
    if not key:
        return None
    if alias_map is not None:
        return alias_map.get(key)
    alias = ResellerAlias.objects.select_related("reseller").filter(alias_norm=key).first()
    return alias.reseller if alias else None


def learn_alias(raw_name: str, reseller: Reseller) -> ResellerAlias:
    """Daftarkan ejaan baru -> reseller (dipakai dari layar review)."""
    return ResellerAlias.objects.update_or_create(
        alias_norm=norm_ref(raw_name),
        defaults={"alias_raw": raw_name.strip(), "reseller": reseller},
    )[0]


def find_matching_rule(
    desc: str,
    channel: str | None = None,
    is_bank: bool = True,
    rules: list[ExclusionRule] | None = None,
) -> ExclusionRule | None:
    """Cari aturan pemisahan aktif yang cocok dengan keterangan transaksi."""
    if not desc:
        return None
    active_rules = rules if rules is not None else list(ExclusionRule.objects.filter(active=True))
    for rule in active_rules:
        if rule.matches(desc, channel=channel, is_bank=is_bank):
            return rule
    return None

