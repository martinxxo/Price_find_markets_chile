"""Telegram (and optional Discord) alert notifier for ChileDeals Radar."""

from __future__ import annotations

import html
import logging
import os
from typing import Optional

import requests

from src.models import DealAlert

logger = logging.getLogger(__name__)


def format_clp(amount: int | float | None) -> str:
    """Format an integer amount in Chilean Pesos (e.g. '$119.990')."""
    if amount is None:
        return "$0"
    return f"${int(round(amount)):,}".replace(",", ".")


def build_telegram_html(alert: DealAlert) -> str:
    """Build an escaped HTML message for Telegram Bot API."""
    s = alert.snapshot
    badge_map = {
        "GLITCH": "🚨 <b>POSIBLE ERROR DE PRECIO (GLITCH)</b>",
        "VIP": "⭐ <b>ALERTA LISTA VIP</b>",
        "FLASH": "⚡ <b>OFERTA FLASH REAL</b>",
        "DOLARES_PREMIO": "✈️ <b>OFERTA DÓLARES PREMIO (TIENDA TRAVEL)</b>",
    }
    header = badge_map.get(alert.alert_type, "🔥 <b>OFERTA DETECTADA</b>")
    title_safe = html.escape(s.title)
    store_safe = html.escape(s.store)
    seller_safe = html.escape(s.seller_name)
    payment_safe = html.escape(s.payment_type)
    reason_safe = html.escape(alert.reason)

    lines = [
        header,
        "",
        f"📦 <b>{title_safe}</b>",
        f"🏬 Tienda: <b>{store_safe}</b> ({seller_safe})",
        f"💳 Medio de pago: <b>{payment_safe}</b>",
        "",
        f"🔥 Precio ahora: <b>{format_clp(s.effective_price)}</b>",
        f"📉 Antes (real): <s>{format_clp(alert.previous_real_price)}</s>",
    ]
    if s.normal_price and s.normal_price > alert.previous_real_price:
        lines.append(f"🏷️ Precio normal tienda: {format_clp(s.normal_price)}")
    if s.dolares_premio_pct > 0:
        lines.append(
            f"✈️ <b>Dólares Premio:</b> hasta {s.dolares_premio_pct}% "
            f"(~{s.dolares_premio_amount:.2f} DP + {format_clp(s.copay_clp)})"
        )
    lines.extend(
        [
            f"💥 Caída real: <b>-{alert.real_drop_pct:.1f}%</b> (ahorras {format_clp(alert.savings_clp)})",
            f"ℹ️ <i>{reason_safe}</i>",
            f"🆔 SKU: <code>{html.escape(s.sku)}</code>",
        ]
    )
    return "\n".join(lines)


class AlertNotifier:
    """Dispatches formatted deal alerts to Telegram and optional Discord webhook."""

    def __init__(
        self,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        discord_webhook_url: Optional[str] = None,
        public_web_url: str = "",
    ) -> None:
        self.telegram_token = (telegram_token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        self.telegram_chat_id = (telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
        self.discord_webhook_url = (
            discord_webhook_url or os.getenv("DISCORD_WEBHOOK_URL", "")
        ).strip()
        self.public_web_url = public_web_url.strip()

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def send_alert(self, alert: DealAlert) -> bool:
        """Send an alert to configured channels, or print preview if unconfigured."""
        message_html = build_telegram_html(alert)
        sent_any = False

        if self.telegram_configured:
            buttons = [[{"text": "🛒 Ir a la Oferta", "url": alert.snapshot.url}]]
            if self.public_web_url.startswith("http"):
                buttons[0].append({"text": "🌐 Ver Radar Web", "url": self.public_web_url})

            image_url = (alert.snapshot.image_url or "").strip()
            if image_url.startswith("http"):
                photo_url = f"https://api.telegram.org/bot{self.telegram_token}/sendPhoto"
                photo_payload = {
                    "chat_id": self.telegram_chat_id,
                    "photo": image_url,
                    "caption": message_html[:1024],
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": buttons},
                }
                try:
                    resp = requests.post(photo_url, json=photo_payload, timeout=12)
                    resp.raise_for_status()
                    sent_any = True
                except requests.RequestException as exc:
                    logger.warning(
                        "sendPhoto failed for %s (%s); falling back to sendMessage.",
                        alert.snapshot.sku,
                        exc,
                    )

            if not sent_any:
                payload = {
                    "chat_id": self.telegram_chat_id,
                    "text": message_html,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                    "reply_markup": {"inline_keyboard": buttons},
                }
                url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
                try:
                    resp = requests.post(url, json=payload, timeout=12)
                    resp.raise_for_status()
                    sent_any = True
                except requests.RequestException as exc:
                    logger.warning("Failed sending Telegram message for %s: %s", alert.snapshot.sku, exc)
        else:
            print("-" * 60)
            print(f"[PREVIEW TELEGRAM - {alert.alert_type}] {alert.snapshot.title}")
            print(
                f"Tienda: {alert.snapshot.store} | Ahora: {format_clp(alert.snapshot.effective_price)} "
                f"| Antes: {format_clp(alert.previous_real_price)} (-{alert.real_drop_pct:.1f}%) "
                f"| Pago: {alert.snapshot.payment_type}"
            )
            print(f"Motivo: {alert.reason}")
            print(f"Link: {alert.snapshot.url}")

        if self.discord_webhook_url:
            s = alert.snapshot
            discord_msg = (
                f"**[{alert.alert_type}] {s.title}**\n"
                f"Tienda: {s.store} ({s.payment_type})\n"
                f"Ahora: **{format_clp(s.effective_price)}** | Antes: {format_clp(alert.previous_real_price)} "
                f"(-{alert.real_drop_pct:.1f}%)\n"
                f"{s.url}"
            )
            try:
                resp = requests.post(
                    self.discord_webhook_url,
                    json={"content": discord_msg},
                    timeout=12,
                )
                resp.raise_for_status()
                sent_any = True
            except requests.RequestException as exc:
                logger.warning("Failed sending Discord webhook: %s", exc)

        return sent_any

    def send_test_message(self) -> bool:
        """Send a connectivity test message to Telegram."""
        if not self.telegram_configured:
            print(
                "TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no están configurados en el entorno. "
                "Configúralos para probar el envío real."
            )
            return False

        buttons = []
        if self.public_web_url.startswith("http"):
            buttons.append([{"text": "🌐 Ver Radar Web", "url": self.public_web_url}])

        payload: dict = {
            "chat_id": self.telegram_chat_id,
            "text": (
                "✅ <b>ChileDeals Radar conectado</b>\n\n"
                "Las notificaciones de glitches y ofertas flash en retail chileno "
                "llegarán automáticamente a este chat."
            ),
            "parse_mode": "HTML",
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}

        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        try:
            resp = requests.post(url, json=payload, timeout=12)
            resp.raise_for_status()
            print("Mensaje de prueba enviado con éxito a Telegram.")
            return True
        except requests.RequestException as exc:
            logger.error("Error enviando mensaje de prueba a Telegram: %s", exc)
            return False
