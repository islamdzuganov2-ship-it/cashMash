//+------------------------------------------------------------------+
//|  CashMashSymbolSpec.mq5                                          |
//|  Сбор спецификаций символов и параметров счёта.                  |
//|                                                                  |
//|  Назначение: фаза 0 проекта. Отвечает на блокирующие вопросы     |
//|  Q1/Q2 из docs/19-Open-Questions.md — пригоден ли брокер для     |
//|  коротких стопов вообще.                                         |
//|                                                                  |
//|  Запуск: перетащить на график, указать символы через запятую.    |
//|  Результат: <Терминал>/Common/Files/cashmash_symbol_spec.csv     |
//|  плюс сводка в журнал «Эксперты».                                |
//|                                                                  |
//|  Скрипт НИЧЕГО не торгует и ничего не изменяет.                  |
//+------------------------------------------------------------------+
#property copyright "CashMash"
#property version   "1.00"
#property script_show_inputs
#property strict

input string InpSymbols   = "";     // Символы через запятую (пусто = символ графика)
input bool   InpWriteCsv  = true;   // Писать CSV в общую папку
input int    InpSpreadSamples = 0;  // Сэмплов спреда для статистики (0 = пропустить)

//+------------------------------------------------------------------+
string TradeModeStr(long v)
{
   switch((ENUM_SYMBOL_TRADE_MODE)v)
   {
      case SYMBOL_TRADE_MODE_DISABLED:  return "DISABLED";
      case SYMBOL_TRADE_MODE_LONGONLY:  return "LONG_ONLY";
      case SYMBOL_TRADE_MODE_SHORTONLY: return "SHORT_ONLY";
      case SYMBOL_TRADE_MODE_CLOSEONLY: return "CLOSE_ONLY";
      case SYMBOL_TRADE_MODE_FULL:      return "FULL";
   }
   return "UNKNOWN";
}

string ExeModeStr(long v)
{
   switch((ENUM_SYMBOL_TRADE_EXECUTION)v)
   {
      case SYMBOL_TRADE_EXECUTION_REQUEST:  return "REQUEST";
      case SYMBOL_TRADE_EXECUTION_INSTANT:  return "INSTANT";
      case SYMBOL_TRADE_EXECUTION_MARKET:   return "MARKET";
      case SYMBOL_TRADE_EXECUTION_EXCHANGE: return "EXCHANGE";
   }
   return "UNKNOWN";
}

string CalcModeStr(long v)
{
   switch((ENUM_SYMBOL_CALC_MODE)v)
   {
      case SYMBOL_CALC_MODE_FOREX:              return "FOREX";
      case SYMBOL_CALC_MODE_FOREX_NO_LEVERAGE:  return "FOREX_NO_LEVERAGE";
      case SYMBOL_CALC_MODE_FUTURES:            return "FUTURES";
      case SYMBOL_CALC_MODE_CFD:                return "CFD";
      case SYMBOL_CALC_MODE_CFDINDEX:           return "CFD_INDEX";
      case SYMBOL_CALC_MODE_CFDLEVERAGE:        return "CFD_LEVERAGE";
      case SYMBOL_CALC_MODE_EXCH_STOCKS:        return "EXCH_STOCKS";
      case SYMBOL_CALC_MODE_EXCH_FUTURES:       return "EXCH_FUTURES";
   }
   return "OTHER(" + IntegerToString(v) + ")";
}

string FillingStr(long mask)
{
   string s = "";
   if((mask & SYMBOL_FILLING_FOK) != 0) s += "FOK ";
   if((mask & SYMBOL_FILLING_IOC) != 0) s += "IOC ";
   if(s == "") s = "RETURN_ONLY";
   return s;
}

string MarginModeStr(long v)
{
   switch((ENUM_ACCOUNT_MARGIN_MODE)v)
   {
      case ACCOUNT_MARGIN_MODE_RETAIL_NETTING: return "RETAIL_NETTING";
      case ACCOUNT_MARGIN_MODE_RETAIL_HEDGING: return "RETAIL_HEDGING";
      case ACCOUNT_MARGIN_MODE_EXCHANGE:       return "EXCHANGE";
   }
   return "UNKNOWN";
}

//+------------------------------------------------------------------+
//| Единицы измерения свопа. Без этого числа swap_long/swap_short     |
//| нечитаемы: -12.50 может означать и пункты, и валюту депозита, и   |
//| годовой процент — разница в стоимости переноса на порядок.        |
//+------------------------------------------------------------------+
string SwapModeStr(long v)
{
   switch((ENUM_SYMBOL_SWAP_MODE)v)
   {
      case SYMBOL_SWAP_MODE_DISABLED:          return "DISABLED";
      case SYMBOL_SWAP_MODE_POINTS:            return "POINTS";
      case SYMBOL_SWAP_MODE_CURRENCY_SYMBOL:   return "CURRENCY_SYMBOL";
      case SYMBOL_SWAP_MODE_CURRENCY_MARGIN:   return "CURRENCY_MARGIN";
      case SYMBOL_SWAP_MODE_CURRENCY_DEPOSIT:  return "CURRENCY_DEPOSIT";
      case SYMBOL_SWAP_MODE_INTEREST_CURRENT:  return "INTEREST_CURRENT";
      case SYMBOL_SWAP_MODE_INTEREST_OPEN:     return "INTEREST_OPEN";
      case SYMBOL_SWAP_MODE_REOPEN_CURRENT:    return "REOPEN_CURRENT";
      case SYMBOL_SWAP_MODE_REOPEN_BID:        return "REOPEN_BID";
   }
   return "UNKNOWN(" + IntegerToString(v) + ")";
}

//+------------------------------------------------------------------+
//| Экспирация — только у биржевых фьючерсов. Для CFD пусто.          |
//+------------------------------------------------------------------+
string ExpirationStr(const string sym)
{
   datetime exp = (datetime)SymbolInfoInteger(sym, SYMBOL_EXPIRATION_TIME);
   if(exp <= 0) return "";
   return TimeToString(exp, TIME_DATE);
}

//+------------------------------------------------------------------+
//| Стоимость движения на N пунктов для 1.0 лота, в валюте счёта     |
//+------------------------------------------------------------------+
double MoneyPerPoints(const string sym, double vol, double points, bool is_long)
{
   double pt = SymbolInfoDouble(sym, SYMBOL_POINT);
   if(pt <= 0.0) return 0.0;

   double open  = SymbolInfoDouble(sym, is_long ? SYMBOL_ASK : SYMBOL_BID);
   if(open <= 0.0) return 0.0;

   double close = is_long ? open - points * pt : open + points * pt;
   double profit = 0.0;

   if(!OrderCalcProfit(is_long ? ORDER_TYPE_BUY : ORDER_TYPE_SELL,
                       sym, vol, open, close, profit))
      return 0.0;

   return MathAbs(profit);
}

//+------------------------------------------------------------------+
void OnStart()
{
   string list = InpSymbols;
   StringTrimLeft(list);
   StringTrimRight(list);
   if(list == "") list = _Symbol;

   string syms[];
   int n = StringSplit(list, ',', syms);
   if(n <= 0) { Print("Не удалось разобрать список символов"); return; }

   int fh = INVALID_HANDLE;
   if(InpWriteCsv)
   {
      fh = FileOpen("cashmash_symbol_spec.csv",
                    FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_COMMON, ';');
      if(fh == INVALID_HANDLE)
         PrintFormat("Не удалось открыть CSV, код %d. Продолжаю только в журнал.",
                     GetLastError());
   }

   // --- Счёт -------------------------------------------------------
   PrintFormat("=== СЧЁТ ===");
   PrintFormat("Сервер: %s | Компания: %s",
               AccountInfoString(ACCOUNT_SERVER),
               AccountInfoString(ACCOUNT_COMPANY));
   PrintFormat("Валюта: %s | Плечо: 1:%d | Режим маржи: %s",
               AccountInfoString(ACCOUNT_CURRENCY),
               (int)AccountInfoInteger(ACCOUNT_LEVERAGE),
               MarginModeStr(AccountInfoInteger(ACCOUNT_MARGIN_MODE)));
   PrintFormat("Тип счёта: %s | Советники разрешены: %s | Торговля разрешена: %s",
               (AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL ? "REAL" :
                AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO ? "DEMO" : "CONTEST"),
               (AccountInfoInteger(ACCOUNT_TRADE_EXPERT)  ? "да" : "НЕТ"),
               (AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ? "да" : "НЕТ"));
   PrintFormat("Stop out: %.2f (%s) | Margin call: %.2f",
               AccountInfoDouble(ACCOUNT_MARGIN_SO_SO),
               (AccountInfoInteger(ACCOUNT_MARGIN_SO_MODE) == ACCOUNT_STOPOUT_MODE_PERCENT ? "%" : "money"),
               AccountInfoDouble(ACCOUNT_MARGIN_SO_CALL));
   PrintFormat("Время сервера: %s | GMT: %s | смещение: %+.1f ч",
               TimeToString(TimeTradeServer(), TIME_DATE | TIME_SECONDS),
               TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS),
               (double)(TimeTradeServer() - TimeGMT()) / 3600.0);

   if(fh != INVALID_HANDLE)
   {
      FileWrite(fh, "symbol", "digits", "point", "tick_size", "tick_value",
                "contract_size", "calc_mode", "trade_mode", "exec_mode",
                "stops_level_pt", "freeze_level_pt", "spread_now_pt", "spread_float",
                "vol_min", "vol_max", "vol_step", "filling_mask",
                "swap_long", "swap_short", "swap_3days", "swap_mode",
                "margin_initial", "expiration", "bid",
                "money_per_10pt_001lot", "verdict");
   }

   // --- Символы ----------------------------------------------------
   for(int i = 0; i < n; i++)
   {
      string s = syms[i];
      StringTrimLeft(s);
      StringTrimRight(s);
      if(s == "") continue;

      if(!SymbolSelect(s, true))
      {
         PrintFormat("!!! Символ %s недоступен", s);
         continue;
      }

      MqlTick tick;
      if(!SymbolInfoTick(s, tick))
      {
         PrintFormat("!!! Нет тика по %s", s);
         continue;
      }

      int    digits = (int)SymbolInfoInteger(s, SYMBOL_DIGITS);
      double point  = SymbolInfoDouble(s, SYMBOL_POINT);
      long   stops  = SymbolInfoInteger(s, SYMBOL_TRADE_STOPS_LEVEL);
      long   freeze = SymbolInfoInteger(s, SYMBOL_TRADE_FREEZE_LEVEL);
      double spread = (point > 0.0) ? (tick.ask - tick.bid) / point : 0.0;
      long   fmask  = SymbolInfoInteger(s, SYMBOL_FILLING_MODE);

      double money10 = MoneyPerPoints(s, 0.01, 10.0, true);

      // Вердикт пригодности для коротких стопов
      string verdict = "OK";
      if(stops > 10)                verdict = "ПЛОХО: stops_level " + IntegerToString(stops) + " pt";
      else if(stops > 5)            verdict = "ВНИМАНИЕ: stops_level " + IntegerToString(stops) + " pt";
      if(spread > 20.0)             verdict += " | широкий спред сейчас";
      if(SymbolInfoInteger(s, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_FULL)
                                    verdict += " | торговля ограничена";
      if((fmask & SYMBOL_FILLING_IOC) == 0 && (fmask & SYMBOL_FILLING_FOK) == 0)
                                    verdict += " | только RETURN";

      PrintFormat("--- %s ---", s);
      PrintFormat("  digits=%d point=%.*f tick_size=%.*f contract=%.2f calc=%s",
                  digits, digits, point, digits,
                  SymbolInfoDouble(s, SYMBOL_TRADE_TICK_SIZE),
                  SymbolInfoDouble(s, SYMBOL_TRADE_CONTRACT_SIZE),
                  CalcModeStr(SymbolInfoInteger(s, SYMBOL_TRADE_CALC_MODE)));
      PrintFormat("  STOPS_LEVEL=%d pt   FREEZE_LEVEL=%d pt   <-- ключевые для скальпинга",
                  (int)stops, (int)freeze);
      PrintFormat("  спред сейчас=%.1f pt   исполнение=%s   режим=%s",
                  spread,
                  ExeModeStr(SymbolInfoInteger(s, SYMBOL_TRADE_EXEMODE)),
                  TradeModeStr(SymbolInfoInteger(s, SYMBOL_TRADE_MODE)));
      PrintFormat("  объём: min=%.3f max=%.2f step=%.3f   filling: %s",
                  SymbolInfoDouble(s, SYMBOL_VOLUME_MIN),
                  SymbolInfoDouble(s, SYMBOL_VOLUME_MAX),
                  SymbolInfoDouble(s, SYMBOL_VOLUME_STEP),
                  FillingStr(fmask));
      PrintFormat("  swap long=%.2f short=%.2f  единицы=%s  тройной день=%d",
                  SymbolInfoDouble(s, SYMBOL_SWAP_LONG),
                  SymbolInfoDouble(s, SYMBOL_SWAP_SHORT),
                  SwapModeStr(SymbolInfoInteger(s, SYMBOL_SWAP_MODE)),
                  (int)SymbolInfoInteger(s, SYMBOL_SWAP_ROLLOVER3DAYS));
      string exp = ExpirationStr(s);
      if(exp != "")
         PrintFormat("  ЭКСПИРАЦИЯ: %s  <-- фьючерс, нужен ролловер "
                     "(см. research/roll_calendar.py)", exp);
      PrintFormat("  10 пунктов на 0.01 лота = %.4f %s",
                  money10, AccountInfoString(ACCOUNT_CURRENCY));
      PrintFormat("  ВЕРДИКТ: %s", verdict);

      if(fh != INVALID_HANDLE)
      {
         FileWrite(fh, s, digits, DoubleToString(point, digits),
                   DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_SIZE), digits),
                   DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_VALUE), 5),
                   DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_CONTRACT_SIZE), 2),
                   CalcModeStr(SymbolInfoInteger(s, SYMBOL_TRADE_CALC_MODE)),
                   TradeModeStr(SymbolInfoInteger(s, SYMBOL_TRADE_MODE)),
                   ExeModeStr(SymbolInfoInteger(s, SYMBOL_TRADE_EXEMODE)),
                   (int)stops, (int)freeze,
                   DoubleToString(spread, 1),
                   (SymbolInfoInteger(s, SYMBOL_SPREAD_FLOAT) ? "float" : "fixed"),
                   DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_MIN), 3),
                   DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_MAX), 2),
                   DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_STEP), 3),
                   FillingStr(fmask),
                   DoubleToString(SymbolInfoDouble(s, SYMBOL_SWAP_LONG), 2),
                   DoubleToString(SymbolInfoDouble(s, SYMBOL_SWAP_SHORT), 2),
                   (int)SymbolInfoInteger(s, SYMBOL_SWAP_ROLLOVER3DAYS),
                   SwapModeStr(SymbolInfoInteger(s, SYMBOL_SWAP_MODE)),
                   DoubleToString(SymbolInfoDouble(s, SYMBOL_MARGIN_INITIAL), 2),
                   ExpirationStr(s),
                   DoubleToString(tick.bid, digits),
                   DoubleToString(money10, 4),
                   verdict);
      }
   }

   if(fh != INVALID_HANDLE)
   {
      FileClose(fh);
      Print("CSV записан: <Терминал>/Common/Files/cashmash_symbol_spec.csv");
   }

   Print("=== Готово. Пришлите содержимое журнала или CSV. ===");
}
//+------------------------------------------------------------------+
