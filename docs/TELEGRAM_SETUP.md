# Telegram API setup

The app logs in as **your own Telegram user** through MTProto (Telethon). A bot cannot read most private signal groups, but your user account can read the groups you already belong to. The app never joins groups and never bypasses permissions.

1. Go to <https://my.telegram.org>, log in with your phone number and open **API development tools**.
2. Create an application (any name or short name, platform "Desktop"). Copy the **api_id** (a number) and the **api_hash** (32 characters).
3. In TGMT5, open **Sources → Telegram login**:
   - enter the API ID and API hash, then click **Save API credentials** (they are stored in Windows Credential Manager);
   - enter your phone number in international format, then click **Send code**;
   - enter the code Telegram sends you. If you use two-step verification, also enter your password. Click **Sign in**.
4. Click **Load my chats**, select the signal groups or channels, then **Add selected as source** and **Save sources**.
5. For each source you can set an enable/disable switch, a risk multiplier (≤ 1), symbol aliases (`GOLD=XAUUSD, NAS=NAS100`) and a parser template.

Security notes
- The session string is a full login token. It is stored only in the OS keyring and redacted from all logs. To revoke it, use Telegram → Settings → Devices.
- The app does not replay messages it missed while offline (`catch_up` is off). Any stale message that arrives is rejected by the signal-age check.
- Notifications to `me` go to your *Saved Messages*.
