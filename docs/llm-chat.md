---
geometry: margin=2cm
fontsize: 11pt
mainfont: "Segoe UI"
monofont: "Consolas"
header-includes:
  - \usepackage{xcolor}
  - \usepackage{mdframed}
  - \usepackage{fancyvrb}
  - \definecolor{userbg}{RGB}{232,243,255}
  - \definecolor{aibg}{RGB}{245,245,245}
  - \definecolor{accentblue}{RGB}{15,52,96}
  - \newmdenv[backgroundcolor=userbg,linewidth=0pt,innerleftmargin=10pt,innerrightmargin=10pt,innertopmargin=8pt,innerbottommargin=8pt,roundcorner=5pt]{userbox}
  - \newmdenv[backgroundcolor=aibg,linewidth=0pt,innerleftmargin=10pt,innerrightmargin=10pt,innertopmargin=8pt,innerbottommargin=8pt,roundcorner=5pt]{aibox}
---

# Чат с LLM: разработка ERC-4626 Yield Vault

## Использование генеративных моделей

- **Модель:** ChatGPT-4o (OpenAI, 2025).
- **Доля кода, сгенерированного моделью:** ~20--25% (ниже 30%).
- **Где использовалось:**
  - черновая генерация шаблонного кода Solidity (заготовки контрактов, интерфейсы адаптеров);
  - помощь с оформлением тестов Foundry и настройкой fuzz/invariant-конфигурации;
  - проверка типовых практик работы с OpenZeppelin, ERC-4626, EIP-712;
  - помощь в написании Python-агента (структура scoring-модуля, EMA-логика);
  - проверка формул нормализации ставок (Aave RAY, Compound per-second).
- **Основные источники, на которые опирался:**
  - документация OpenZeppelin: https://docs.openzeppelin.com/contracts/5.x/
  - стандарт ERC-4626: https://eips.ethereum.org/EIPS/eip-4626
  - стандарт EIP-712: https://eips.ethereum.org/EIPS/eip-712
  - документация Aave V3: https://docs.aave.com/developers/
  - документация Compound V3 (Comet): https://docs.compound.finance/
  - документация Foundry: https://book.getfoundry.sh/
  - документация Chainlink Automation: https://docs.chain.link/chainlink-automation
  - материалы курса и формулировка задания.
- **Пример промпта:**
  *«Сгенерируй черновой шаблон ERC-4626 vault с UUPS proxy и ReentrancyGuard. Нужны функции deposit, withdraw, rebalance. Не меняй архитектуру; финальные правки и проверку безопасности оставь за мной.»*
- **Опыт использования / ограничения:**
  LLM ускорила рутинные части (boilerplate-код, конфигурация тестов), но потребовались ручные правки: проверка порядка наследования OpenZeppelin, корректность _decimalsOffset для inflation attack protection, правильная реализация EIP-712 domain separator, настройка Foundry invariant handler.
- **Ответственность за решения:**
  **Все ключевые решения (архитектура vault, формулы MCDM-модели, выбор весов скоринга, дизайн системы безопасности и интерпретация результатов тестирования) принимались мной; модель использовалась как ассистент.**

\newpage

# Фрагменты диалога

Ниже приведены ключевые фрагменты переписки с LLM, демонстрирующие процесс разработки.

---

## Диалог 1: Архитектура vault

\begin{userbox}
\textbf{Пользователь:}

Мне нужно спроектировать DeFi vault, который автоматически перекладывает USDC между Aave V3 и Compound V3 для максимизации доходности. Vault должен быть ERC-4626 совместимым, upgradeable (UUPS proxy), и иметь защиту от inflation attack. Какую архитектуру ты бы предложил?
\end{userbox}

\begin{aibox}
\textbf{ChatGPT:}

Для вашего случая рекомендую трехуровневую архитектуру:

\textbf{1. AIVault.sol} -- основной контракт vault:
\begin{itemize}
\item Наследует ERC4626Upgradeable, UUPSUpgradeable, ReentrancyGuardUpgradeable
\item Хранит shares пользователей, управляет deposit/withdraw
\item Для защиты от inflation attack используйте \texttt{\_decimalsOffset() = 6}, это добавит $10^6$ виртуальных shares
\end{itemize}

\textbf{2. StrategyManager.sol} -- логика принятия решений:
\begin{itemize}
\item Проверяет подписи агента (EIP-712)
\item Хранит nonce, cooldown, параметры стратегии
\item Отделен от vault для возможности апгрейда логики отдельно
\end{itemize}

\textbf{3. IProtocolAdapter} -- интерфейс для протоколов:
\begin{itemize}
\item AaveV3Adapter -- обертка над Aave V3 Pool
\item CompoundV3Adapter -- обертка над Compound Comet
\item Новые протоколы добавляются без изменения vault
\end{itemize}

Для inflation attack: стандартная защита через virtual shares offset. OpenZeppelin ERC4626 уже поддерживает \texttt{\_decimalsOffset}, просто переопределите его на 6.
\end{aibox}

\begin{userbox}
\textbf{Пользователь:}

Хорошо. А как быть если агент упадет или будет недоступен? Vault не должен оставаться без управления.
\end{userbox}

\begin{aibox}
\textbf{ChatGPT:}

Рекомендую добавить fallback через Chainlink Automation:

\begin{itemize}
\item Реализуйте интерфейс \texttt{AutomationCompatibleInterface} с функциями \texttt{checkUpkeep} и \texttt{performUpkeep}
\item В \texttt{checkUpkeep} проверяйте: прошло ли больше 6 часов с последней ребалансировки И есть ли экономическая выгода от переключения
\item Формула выгоды: \texttt{dailyBenefit = TVL * deltaAPY / 365 days}
\item Ребалансировка через Chainlink происходит только если \texttt{benefitOverPeriod > estimatedGasCost}
\end{itemize}

Так у вас будет два пути: основной (агент с MCDM) и fallback (Chainlink с простой cost-benefit проверкой).
\end{aibox}

---

## Диалог 2: Формулы MCDM-модели

\begin{userbox}
\textbf{Пользователь:}

Я не хочу просто сравнивать APY между протоколами -- это наивный подход. Нужна многофакторная модель. Какие факторы стоит учитывать для скоринга lending-протоколов?
\end{userbox}

\begin{aibox}
\textbf{ChatGPT:}

Для MCDM (Multi-Criteria Decision Making) модели в контексте DeFi lending рекомендую 4 фактора:

\textbf{1. APY (вес 40\%)} -- основной сигнал доходности. Нормализуйте к диапазону [0, 1] через деление на максимальный ожидаемый APY (например, 20\%).

\textbf{2. Risk (вес 25\%)} -- обратная функция от utilization rate. Высокая утилизация (>90\%) означает риск скачка ставок и проблем с ликвидностью. Формула: \texttt{f\_risk = 1 - normalize(utilization, 0, 1)}.

\textbf{3. Cost (вес 20\%)} -- стоимость газа для ребалансировки относительно TVL. Формула: \texttt{f\_cost = 1 - normalize(gasCost/TVL, 0, 0.01)}.

\textbf{4. Stability (вес 15\%)} -- волатильность TVL протокола. Резкое падение TVL может означать проблемы. Формула: \texttt{f\_stability = 1 - normalize(abs(deltaTVL), 0, 0.30)}.

Итоговый скор: $Score_i = 0.40 \cdot f_{APY} + 0.25 \cdot f_{Risk} + 0.20 \cdot f_{Cost} + 0.15 \cdot f_{Stability}$

Ребалансировку выполняйте только если разница скоров $\geq 0.05$, чтобы избежать избыточных переключений.
\end{aibox}

\begin{userbox}
\textbf{Пользователь:}

Веса я подберу сам по результатам тестов. А как сглаживать ставки, чтобы не реагировать на краткосрочные манипуляции?
\end{userbox}

\begin{aibox}
\textbf{ChatGPT:}

Используйте экспоненциальное скользящее среднее (EMA):

$$S_t = \alpha \cdot R_t + (1 - \alpha) \cdot S_{t-1}$$

где $\alpha = 0.3$ (30\% веса на новое наблюдение). Это хороший баланс между отзывчивостью и стабильностью.

Дополнительно добавьте \textbf{jump guard}: если ставка изменилась больше чем на 5\% (500 bps) за один период, пропустите обновление EMA. Это защита от flash loan атак, когда атакующий может искусственно завысить ставку на один блок.

Реализация на Solidity: храните \texttt{smoothedRate} в mapping по адресу протокола, обновляйте при каждом вызове \texttt{updateRate}.
\end{aibox}

---

## Диалог 3: Шаблон ERC-4626 vault

\begin{userbox}
\textbf{Пользователь:}

Сгенерируй черновой шаблон AIVault.sol с наследованием от OpenZeppelin 5.x. Нужны: ERC4626, UUPS, ReentrancyGuard, Ownable. Функцию rebalance пока оставь пустой, я допишу сам.
\end{userbox}

\begin{aibox}
\textbf{ChatGPT:}

\begin{Verbatim}[fontsize=\small]
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts-upgradeable/token/ERC20/
  extensions/ERC4626Upgradeable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/
  UUPSUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/utils/
  ReentrancyGuardUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/access/
  OwnableUpgradeable.sol";

contract AIVault is
    ERC4626Upgradeable,
    UUPSUpgradeable,
    ReentrancyGuardUpgradeable,
    OwnableUpgradeable
{
    IStrategyManager public strategyManager;
    uint256 public lastRebalance;
    uint256 public constant COOLDOWN = 1 hours;

    function initialize(
        IERC20 asset_,
        address strategyManager_,
        address owner_
    ) external initializer {
        __ERC4626_init(asset_);
        __ERC20_init("AI Yield USDC", "aiUSDC");
        __UUPSUpgradeable_init();
        __ReentrancyGuard_init();
        __Ownable_init(owner_);
        strategyManager =
          IStrategyManager(strategyManager_);
    }

    function _decimalsOffset()
        internal pure override returns (uint8)
    {
        return 6; // inflation attack protection
    }

    function rebalance(bytes calldata signature)
        external nonReentrant
    {
        // TODO: verify signature, check cooldown,
        // execute via adapter
    }

    function _authorizeUpgrade(address)
        internal override onlyOwner {}
}
\end{Verbatim}

Порядок наследования важен: ERC4626 первым (из-за linearization), затем UUPS, ReentrancyGuard, Ownable. Проверьте что \texttt{\_decimalsOffset = 6} дает $10^6$ виртуальных shares.
\end{aibox}

\begin{userbox}
\textbf{Пользователь:}

Порядок наследования проверил, все корректно. Функцию rebalance я реализовал сам с EIP-712 верификацией и cooldown-проверкой. Спасибо за шаблон.
\end{userbox}

---

## Диалог 4: Нормализация ставок

\begin{userbox}
\textbf{Пользователь:}

Aave V3 хранит ставки в формате RAY ($10^{27}$), а Compound V3 -- per-second rate. Мне нужно привести оба к единому годовому формату в масштабе 1e18. Как правильно сделать конвертацию?
\end{userbox}

\begin{aibox}
\textbf{ChatGPT:}

Для Aave V3:
\begin{Verbatim}[fontsize=\small]
// liquidityRate is in RAY (1e27)
// Target: APY in 1e18 scale
// Simply divide by 1e9:
uint256 apyE18 = liquidityRate / 1e9;
\end{Verbatim}

Для Compound V3 (Comet):
\begin{Verbatim}[fontsize=\small]
// getSupplyRate returns per-second rate
// in 1e18 scale
// Multiply by seconds per year:
uint256 SECONDS_PER_YEAR = 31_557_600;
// (365.25 days, accounts for leap years)
uint256 apyE18 = supplyRate * SECONDS_PER_YEAR;
\end{Verbatim}

Важно: используйте 31,557,600 секунд (365.25 дней), а не 31,536,000 (365 дней), чтобы корректно учесть високосные годы. Разница небольшая, но для точности формул это важно.

Вынесите обе конвертации в отдельную библиотеку \texttt{RateMath.sol} для переиспользования.
\end{aibox}

---

## Диалог 5: Настройка Foundry invariant-тестов

\begin{userbox}
\textbf{Пользователь:}

Я написал unit-тесты и интеграционные тесты. Теперь хочу добавить invariant-тесты через Foundry. Нужно проверить 6 инвариантов: solvency, accounting consistency, share price monotonicity, round-trip conversions, supply consistency, rounding direction. Как настроить handler?
\end{userbox}

\begin{aibox}
\textbf{ChatGPT:}

Создайте handler-контракт, который Foundry будет вызывать случайным образом:

\begin{Verbatim}[fontsize=\small]
contract VaultHandler is Test {
    AIVault vault;
    address[] actors;

    function deposit(uint256 actorSeed,
        uint256 amount) external {
        address actor = actors[
          actorSeed % actors.length];
        amount = bound(amount, 1e6, 1_000_000e6);
        vm.startPrank(actor);
        usdc.approve(address(vault), amount);
        vault.deposit(amount, actor);
        vm.stopPrank();
    }

    function withdraw(uint256 actorSeed,
        uint256 amount) external {
        // similar with bound()
    }

    function rebalance() external {
        // simulate agent rebalance
    }
}
\end{Verbatim}

В \texttt{foundry.toml}:
\begin{Verbatim}[fontsize=\small]
[invariant]
runs = 128
depth = 600
\end{Verbatim}

Это даст 128 прогонов $\times$ 600 вызовов = 76,800 случайных действий. Каждый инвариант проверяется после каждого вызова.

Для проверки solvency: \texttt{totalAssets() >= sum(maxWithdraw(actor))} для всех акторов.
\end{aibox}

\begin{userbox}
\textbf{Пользователь:}

Настроил handler по этому шаблону, добавил все 6 инвариантов. После 76,800 вызовов -- 0 нарушений. Все инварианты выполняются.
\end{userbox}

---

## Диалог 6: Python scoring-модуль

\begin{userbox}
\textbf{Пользователь:}

Помоги написать черновик scoring.py для Python-агента. Нужна функция score\_protocols, которая принимает список протоколов с метриками и возвращает скоры по MCDM-модели. Веса: APY 0.40, Risk 0.25, Cost 0.20, Stability 0.15.
\end{userbox}

\begin{aibox}
\textbf{ChatGPT:}

\begin{Verbatim}[fontsize=\small]
WEIGHTS = {
    "apy": 0.40,
    "risk": 0.25,
    "cost": 0.20,
    "stability": 0.15,
}

def normalize(value, min_val, max_val):
    clamped = max(min_val, min(value, max_val))
    return clamped / (max_val - min_val)

def score_protocol(metrics, gas_price):
    f_apy = normalize(metrics["apy"], 0, 0.20)
    f_risk = 1 - normalize(
        metrics["utilization"], 0, 1)
    f_cost = 1 - normalize(
        gas_price / metrics["tvl"], 0, 0.01)
    f_stability = 1 - normalize(
        abs(metrics["tvl_delta"]), 0, 0.30)

    return (WEIGHTS["apy"] * f_apy
          + WEIGHTS["risk"] * f_risk
          + WEIGHTS["cost"] * f_cost
          + WEIGHTS["stability"] * f_stability)

def score_protocols(protocols, gas_price):
    scores = {}
    for name, metrics in protocols.items():
        scores[name] = score_protocol(
            metrics, gas_price)
    best = max(scores, key=scores.get)
    return scores, best
\end{Verbatim}

Рекомендую добавить порог: ребалансировка только если \texttt{scores[best] - scores[current] >= 0.05}.
\end{aibox}

\begin{userbox}
\textbf{Пользователь:}

Взял за основу, доработал: добавил EMA-сглаживание ставок перед скорингом, логирование, и проверку порога 0.05. Также написал 20 unit-тестов на pytest для граничных случаев.
\end{userbox}

---

## Диалог 7: EIP-712 подпись в Python

\begin{userbox}
\textbf{Пользователь:}

Агент должен подписывать решение о ребалансировке через EIP-712. Как реализовать подпись typed data в Python с web3.py?
\end{userbox}

\begin{aibox}
\textbf{ChatGPT:}

Используйте \texttt{eth\_account} (входит в web3.py):

\begin{Verbatim}[fontsize=\small]
from eth_account import Account
from eth_account.messages import encode_typed_data

domain = {
    "name": "AIVault",
    "version": "1",
    "chainId": 11155111,
    "verifyingContract": vault_address,
}

types = {
    "Rebalance": [
        {"name": "targetAdapter", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "timestamp", "type": "uint256"},
    ]
}

message = {
    "targetAdapter": target_adapter,
    "nonce": current_nonce,
    "timestamp": int(time.time()),
}

signable = encode_typed_data(
    domain, types, message)
signed = Account.sign_message(
    signable, private_key)
signature = signed.signature.hex()
\end{Verbatim}

На стороне Solidity используйте \texttt{ECDSA.recover(digest, signature)} от OpenZeppelin для верификации.
\end{aibox}

---

## Итог

LLM использовалась как ассистент для ускорения рутинных задач. Все архитектурные решения, формулы MCDM-модели, дизайн системы безопасности, финальная реализация и тестирование выполнены самостоятельно. Сгенерированный код составляет не более 20--25\% от общего объема и был существенно переработан.
