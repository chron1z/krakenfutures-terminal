# krakenfutures-terminal
A lightweight, real-time trading terminal for Kraken Futures.

Features:

- Low memory, lightweight application (~70MB compared to ~700MB for Kraken's web terminal)
- Real-time WebSocket price data and order book updates (significantly faster than Kraken's web terminal)
- 1-minute volume tracking
- Position management with unrealized P&L tracking
- Smart price selection: best price/mid price
- Instant market close position with one-click market orders
- Recent trades display
- Bulk order cancellation
- Connection status indicator
- Dark mode

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

![image](https://github.com/user-attachments/assets/80b02291-479b-4142-9884-259014909bba)


DISCLAIMER:
This software is for informational purposes only. Use at your own risk. 
The authors and contributors are not responsible for any trading losses, damages or other issues that may occur when using this terminal. 
Always verify order details before trading and never risk more than you can afford to lose.

