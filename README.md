# krakenfutures-terminal
A lightweight, real-time trading terminal for Kraken Futures.

Features:

- Low memory, lightweight application
- Real-time WebSocket price data and order book updates
- 1-minute volume tracking
- Position management with unrealized P&L tracking
- Smart price selection: Best price/Mid price
- Fast position exit with one-click market orders
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

![image](https://github.com/user-attachments/assets/9498e637-fc12-42e3-8b1d-bc21af4371f7)
