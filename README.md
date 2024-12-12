# krakenfutures-terminal
A lightweight, real-time trading terminal for Kraken Futures.

Features:

- Low memory, lightweight application (~25MB compared to ~700MB for Kraken's web terminal)
- Real-time WebSocket price, order and position data (significantly faster than Kraken's web terminal)
- Unrealized P&L tracking with best/mid price, market order price impact calculation
- Smart price selection: best price/mid price 
- Instant market close position with one-click market orders
- 1-minute volume tracking
- Recent trades display
- Bulk order cancellation
- Connection status indicator
- Dark mode theme

Using ccxt for exchange connectivity, PyQt5 for user interface

Usage:
- Add API credentials to settings.py and set font size as desired for your screen size
- Run main.py

Changelog:
- Added dark-mode theme
- Added balance, available margin and required margin
- Added ARM button
- Added customizable ticker quick-swap bar
- Added customizable data throttling for websockets
- Added mid, best and market impact price to UPNL calculations

![image](https://github.com/user-attachments/assets/19c5cf74-426c-423d-8b1f-2229cac85133)

DISCLAIMER:
This software is for informational purposes only. Use at your own risk. 
The authors and contributors are not responsible for any trading losses, damages or other issues that may occur when using this terminal.

