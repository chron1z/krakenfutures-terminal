# KrakenFutures Terminal 

A high-performance, real-time trading terminal for Kraken Futures built with Python.

## Key Features

### Performance
- Lightweight application (~25MB vs ~700MB for Kraken's web terminal)
- Real-time WebSocket data streaming
- Fast execution and responsive UI

### Trading Tools
- Smart price selection (best/mid price)
- One-click market close position
- Bulk order cancellation
- Customizable hotkeys
- Real-time P&L tracking with:
  - Best price
  - Mid price
  - Market impact calculation

### Market Data
- 1-minute volume tracking
- Recent trades display
- Real-time orderbook
- Connection status indicator

### UI/UX
- Dark/Light theme
- Customizable quick-swap tickers
- Adjustable data throttling

## Technology Stack
- Python
- ccxt for exchange connectivity
- PyQt5 for user interface
- WebSocket for real-time data

## Getting Started

1. Configure API credentials in settings.py
2. Adjust font size for your display
3. Run main.py

## Recent Updates
- Added dark mode theme
- Implemented balance and margin tracking
- Added ARM safety button
- Introduced customizable ticker quick-swap
- Enhanced UPNL calculations (market price impact)
- Added configurable hotkeys
- Improved price/quantity input controls

![Terminal Screenshot](https://github.com/user-attachments/assets/96871c60-1561-4eb6-8a90-cc44f5d14818)

## Disclaimer
This software is for informational purposes only. Use at your own risk. The authors and contributors assume no responsibility for trading losses or other damages.
