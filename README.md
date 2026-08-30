# Autonomous Quant Agent

> **Semester project blueprint — Nasdaq semiconductor and network-infrastructure equities, long/short/flat decisions, and Alpaca paper trading only**

Autonomous Quant Agent is an educational quantitative-research platform built around one narrow experiment: whether a pooled machine-learning model can produce useful 15-minute long, short, or flat decisions within a fixed eight-stock semiconductor and network-infrastructure universe. It will collect historical and real-time data, generate bounded candidate variations, train them, evaluate them with a deterministic backtester, and promote only qualified versions into Alpaca paper trading.

The system will never support real-money execution. Market data may be real, but account capital and fills are simulated by Alpaca Paper Trading.

## Project definition

**This is not a general market-regime detection system.** The existing prototype contains a regime-aware allocator, but regime discovery and broad-market rotation are not part of the target MVP or its final results.

### Research question

> Can one pooled XGBoost classifier, using 15-minute technical and cross-sectional features, outperform predeclared passive and active baselines after spread, slippage, turnover, and conservative short-cost assumptions within a fixed basket of eight Nasdaq-listed semiconductor and network-infrastructure equities?

| Supervisor question | Fixed project answer |
| --- | --- |
| What market is being studied? | Eight Nasdaq-listed U.S. equities from the semiconductor, optical-connectivity, broadband, and network-infrastructure value chain |
| What does the model predict? | **LONG**, **SHORT**, or **FLAT** every 15 minutes for a four-bar, approximately one-hour forecast horizon |
| What strategy is employed? | One pooled three-class XGBoost strategy using a fixed approved feature catalog and a target-position execution policy |
| What are the session rules? | Regular hours only; the last model decision is at 2:30 p.m. ET and every strategy/benchmark position is forcibly flat by 3:45 p.m., so nothing is held overnight |
| Does the agent invent strategies freely? | No. We provide the strategy template; the research agent may choose only five registered feature bundles plus finite hyperparameter and decision-threshold grids |
| Does it change itself while trading? | No. Training and revisions are offline; a deployed model is immutable and can change only after a new version passes the full evaluation gate |
| How is it evaluated? | Purged walk-forward folds, one locked holdout, after-cost backtesting, block-bootstrap support, fixed baselines, and predeclared promotion thresholds |
| What is the listing venue? | Nasdaq is the primary listing venue for all eight active securities and is revalidated when each dataset is frozen |
| Where are orders sent? | Only to the Alpaca Paper Trading API; fills are simulated and no securities exchange receives the order |
| What is the engineering stack for? | Docker, Kubernetes, AWS, APIs, storage, and security deliver the experiment; they are not additional trading hypotheses |

### Primary hypothesis

The candidate strategy succeeds only if its causal outer-fold development evidence and one untouched holdout both pass every hard promotion rule later in this README. The development gate includes net Sharpe of at least 0.75, maximum drawdown no greater than 15%, positive results in at least three of four walk-forward folds, at least 95% moving-block-bootstrap support that net Sharpe exceeds 0.50, and net Sharpe at least 0.10 above the best of logistic regression, momentum, and mean reversion under the same risk budget. A development failure may receive only a registered bounded revision; any holdout failure permanently rejects that dataset/candidate generation and can never feed a revision.

## Project status

This README is the final system-design plan. It describes both the existing repository and the system this semester project will become.

The repository already contains a useful **Adaptive Portfolio Agent prototype** with:

- deterministic synthetic backtesting and replay;
- momentum and mean-reversion strategies;
- regime-aware allocation in the legacy prototype only;
- an independent risk engine;
- an Alpaca market-data observer;
- guarded Alpaca paper-trading code;
- immutable decision receipts and SQLite persistence;
- a read-only Streamlit dashboard;
- Docker support and an offline test suite.

Operationally, the checked-in prototype is **offline-verified only**. Paper submission is disabled in every tracked configuration, and the repository contains no credential-based connectivity record, completed real-market observer-session evidence, or real-data dry-run evidence. The presence of observer and guarded paper code is not evidence that either path has been authorized or successfully operated.

**No target eight-stock experiment has been run yet.** Legacy synthetic and ETF checks are not results for this proposal. A passing strategy is not required for the semester project to be scientifically complete: measured outperformance and a fully reproducible `REJECTED` result are both valid conclusions, while only a passing result may advance to shadow or paper operation.

The final project extends that foundation. The following **core research capabilities are planned, not currently complete**:

- the fixed eight-stock universe below;
- a central historical and real-time data service;
- immutable Parquet research datasets;
- long, short, and flat ML predictions;
- autonomous but bounded candidate generation;
- purged walk-forward training and evaluation;
- a statistical strategy-confidence gate and revision loop.

The conditional paper/demo target adds one explicitly authorized Alpaca paper account, a local dashboard, the small private control API, and a Docker Compose demonstration only after the research path and safety controls work.

Local Kubernetes and a temporary AWS deployment are engineering showcase goals. Multi-user tenancy, Cognito, Alpaca OAuth, a React frontend, and a public SaaS deployment are later product extensions; they are not required to answer the research question.

The current prototype is long-only. The target project is **not long-only**: its decision set is **LONG**, **SHORT**, or **FLAT**. Shorting will be added explicitly and will require its own borrow-cost assumptions, exposure limits, broker eligibility checks, and failure tests.

## Scope and non-goals

### Core research MVP

- The eight Nasdaq-listed equities in the fixed research universe below
- Historical and real-time Alpaca market data
- Fifteen-minute strategy decisions for the MVP
- Long, short, or flat model outputs
- Offline model training and bounded strategy generation
- Deterministic, after-cost backtesting
- A private central data service
- Reproducible CLI/HTML reports and GitHub Actions

### Paper/demo target — conditional on a passing model

- Run an approved immutable model in real-time shadow mode
- Add independent risk, a kill switch, idempotent order intent, and Alpaca paper reconciliation
- Use one manually authorized Alpaca paper account, separate from the collector account
- Expose Streamlit views and a small private FastAPI job/report API
- Run the complete demonstration with Docker Compose

The code paths and safety tests can be completed even if research rejects every candidate. Actual shadow promotion or submission of an Alpaca paper order occurs only if one candidate passes both development and holdout; a negative result is demonstrated as a safe no-activation outcome.

### Engineering showcase

- Run the same containers in local Kubernetes with k3d or kind
- Create a temporary single-node AWS/k3s environment with Terraform
- Demonstrate container builds, deployment, logging, rollback, and cost controls

These items demonstrate software-engineering experience. They do not affect whether the trading hypothesis passes.

### Product extensions, not MVP requirements

- Multiple users and tenant isolation
- Alpaca OAuth account connections
- Cognito authentication
- A React/Next.js frontend
- A continuously hosted public service

### Out of scope

- Real-money trading
- Options, futures, cryptocurrency, or foreign exchanges
- Any tradable symbol outside the fixed eight-stock MVP universe
- High-frequency or latency-sensitive trading
- Unrestricted AI-generated code execution
- An AI with access to broker credentials or risk settings
- Self-modification while markets are open
- Guaranteed profitability
- Public redistribution of raw Alpaca market data
- Enterprise-scale high availability

The goal is a credible, reproducible answer to one research question—not a hedge fund and not a production brokerage platform.

## MVP research universe

The MVP is intentionally limited to a thematic value chain rather than the general U.S. equity market. The eight companies cover accelerated computing, semiconductors and materials, flash storage, optical connectivity, broadband infrastructure, and 5G network-edge equipment. They are not claimed to share one identical GICS sub-industry; the common research theme is the hardware and connectivity infrastructure used to move, process, and store data.

| Symbol | Company | Role in the research theme | Primary listing venue |
| --- | --- | --- | --- |
| NVDA | NVIDIA Corporation | Accelerated computing and semiconductors | Nasdaq |
| AMD | Advanced Micro Devices, Inc. | CPUs, GPUs, and adaptive computing | Nasdaq |
| CSCO | Cisco Systems, Inc. | Enterprise and data-center networking | Nasdaq |
| SNDK | Sandisk Corporation | Flash storage and memory technology | Nasdaq |
| AAOI | Applied Optoelectronics, Inc. | Optical components for data-center and telecom networks | Nasdaq |
| AXTI | AXT, Inc. | Compound-semiconductor wafer substrates | Nasdaq |
| HLIT | Harmonic Inc. | Broadband and network infrastructure | Nasdaq |
| INSG | Inseego Corp. | 5G edge and fixed-wireless connectivity | Nasdaq |

Primary-listing metadata is revalidated when a dataset is frozen and again before paper operation. Official sources currently identify the listings for [NVDA](https://investor.nvidia.com/investor-resources/faqs/), [AMD](https://ir.amd.com/contacts-faq/faq), [CSCO](https://investor.cisco.com/resources/investor-faqs/default.aspx), [SNDK](https://investor.sandisk.com/ir-resources/investor-faqs), [AAOI](https://investors.ao-inc.com/static-files/d182cb31-353d-496a-85ac-de2ce5632b1e), [AXTI](https://investors.axt.com/Investors/resources/investor-faqs/default.aspx), [HLIT](https://investor.harmonicinc.com/news-releases/news-release-details/harmonic-announces-second-quarter-2026-results), and [INSG](https://investor.inseego.com/).

**Ticker clarification:** SNDK is Sandisk, while Western Digital trades as WDC. The MVP uses SNDK only and never splices WDC history into it. SNDK's shorter post-separation history is handled by the listing-aware eligibility rules described below.

### Benchmark-only and context symbols

- **Primary passive benchmark:** equal-weight intraday-only portfolio of the same eight MVP stocks under the same session and gross-exposure rules
- **Sector context:** SOXX, the iShares Semiconductor ETF, is benchmark-only and excluded from every order allowlist; it is useful but imperfect because several MVP companies are networking rather than pure-semiconductor businesses
- **Broad-market context:** QQQ and SPY may provide lagged market features and contextual reporting but are never traded
- **Active baselines:** fixed momentum, fixed mean-reversion, and multinomial logistic-regression strategies under the same timestamps, costs, and risk budget
- **Safety baseline:** always FLAT

SOXX is a real tradable ETF outside this project; "benchmark-only" describes its role here. The official [iShares SOXX page](https://www.ishares.com/us/products/239705/ishares-semiconductor-etf) states that it tracks a U.S. semiconductor equity index. Because SOXX is long-only and held overnight while this strategy may be long/short with capped exposure, raw SOXX buy-and-hold return is contextual rather than the primary promotion comparison.

### Future-only watchlist

The remaining symbols from the original idea are preserved only as possible future research: `TSLA`, `UBER`, `GOOGL`, `AMZN`, `AAPL`, `META`, `NET`, `OKTA`, `ROKU`, `BOX`, `ZG`, `RBLX`, `SOUN`, `PUBM`, `PAYC`, `WDAY`, `RIVN`, and `LCID`.

They may appear only as exclusion metadata in a dataset manifest. They are excluded from collected bar rows, feature matrices, labels, ranks, training, backtests, predictions, positions, shadow decisions, and paper orders. Moving any future symbol into the active universe requires a new versioned universe, new frozen datasets, a fresh search budget, and the complete evaluation/promotion cycle.

At startup, the system verifies exactly the eight active symbols as Nasdaq-listed, active, and tradable. A short paper decision additionally requires current shortable/easy-to-borrow eligibility. Context symbols are read-only, and future-watchlist symbols cannot enter model or execution contracts.

## Market, data, and execution boundary

| Concept | This project |
| --- | --- |
| Asset class | Nasdaq-listed U.S. common equities in the fixed eight-stock theme |
| Primary listing venue | Nasdaq, revalidated as versioned security metadata |
| Market-data provider | Alpaca Market Data API |
| MVP equity data feed | IEX for both historical and real-time bars |
| MVP adjustment mode | Raw bars in both historical research and live inference; corporate-action boundaries are flagged and excluded from rolling windows |
| Broker and simulator | Alpaca Paper Trading API |
| Supported session | Regular U.S. market hours only, 9:30 a.m.–4:00 p.m. Eastern Time |
| Direct exchange connectivity | None |

Alpaca is the API broker and market-data provider; it is not an exchange. IEX is the selected single-venue data feed, not the full consolidated SIP view. A Nasdaq-listed security can trade on IEX, so primary listing venue and observed data venue are different concepts. The project has no direct connection to Nasdaq, IEX, NYSE, or the SIP; no exchange membership; no direct-market-access or routing control; and no live-money brokerage path.

## Core design decisions

1. **The data collector is centralized and private.** Its Alpaca credential pair belongs to a separate operational account from the approved paper-execution account. It collects the eight active symbols plus the explicitly configured benchmark/context symbols and does not collect the future watchlist for MVP research. Its effective data-only role is enforced primarily by account/secret separation and collector code that has no trading client and rejects trading hostnames. An allowlisting egress proxy or FQDN-aware Cilium policy is an optional cloud hardening layer; ordinary Kubernetes NetworkPolicy alone is not claimed to enforce hostname separation.
2. **MVP trading authority is singular and explicit.** Orders go only to the one approved Alpaca paper account. Per-user OAuth connections are a later product extension.
3. **The database is not the AI.** It is the reliable handoff point between collection, training, testing, and execution.
4. **The AI proposes; deterministic systems decide.** Candidate specifications are schema-validated, the backtester evaluates them, and hard promotion rules control deployment.
5. **The backtester is conventional software, not AI.** Its results must be reproducible.
6. **Signal confidence and strategy confidence are different.** One controls individual trade proposals; the other controls whether a whole strategy may be promoted.
7. **An unknown or unsafe state means no trade.**
8. **The system retrains offline.** The active paper trader never changes itself during a market session.
9. **Docker packages each workload; Kubernetes operates those containers.** Kubernetes is useful for deployment experience, not because the MVP needs massive scale.
10. **The existing repository should be evolved, not erased.** Its risk, replay, receipt, and paper-safety work remains valuable.

## Target system architecture

The diagram shows the full target path. The required research path ends with a promotion or `REJECTED` decision; shadow and paper nodes run only for a holdout-passed artifact. The control API and web layer support the conditional demonstration, while multi-user tenancy is a future extension.

~~~mermaid
flowchart TB
    USER["MVP operator"] --> WEB["Web dashboard"]
    WEB -->|"Authenticated control request"| API["Private FastAPI control API"]

    ALPACA_DATA["Alpaca Market Data API: historical REST + real-time IEX WebSocket"] --> COLLECTOR["Private market-data collector"]
    COLLECTOR --> PG["PostgreSQL: current data + metadata"]
    COLLECTOR --> OBJECTS["Private object storage: Parquet + artifacts"]

    API --> JOBS["PostgreSQL job + outbox store"]
    JOBS --> RESEARCH["Bounded research agent"]
    OBJECTS --> RESEARCH
    RESEARCH --> TRAINER["Feature pipeline + model trainer"]
    TRAINER --> BACKTEST["Deterministic backtester"]
    BACKTEST --> GATE{"Confidence + hard promotion gate"}
    GATE -->|"Development failure only: bounded feedback"| RESEARCH
    GATE -->|"Development + holdout passed"| REGISTRY["Model registry"]

    REGISTRY --> INFERENCE["Live inference"]
    PG --> INFERENCE
    INFERENCE --> RISK["Independent risk engine"]
    RISK --> EXECUTOR["Paper-only executor"]
    EXECUTOR -->|"Approved paper credential"| ALPACA_PAPER["Alpaca Paper Trading API: simulated account, orders, and fills"]
    ALPACA_PAPER --> RECON["Order/fill reconciliation"]
    EXECUTOR --> RECON
    RECON --> PG

    SECRETS["Secrets Manager / encrypted token store"] --> COLLECTOR
    SECRETS --> EXECUTOR
    PG --> API
~~~

Only the website and control API are internet-facing. Market data, datasets, models, job/event stores, databases, research workers, risk checks, and broker tokens remain private.

## Component responsibilities and trust boundaries

| Component | Responsibility | May access | Must not access |
| --- | --- | --- | --- |
| Web dashboard | Show jobs, reports, models, paper positions, and system health | MVP control API | Database passwords, AWS keys, broker tokens |
| Control API | Authenticate the operator, validate requests, create jobs, and return results | Operator metadata, job/outbox records, safe read models | Broker-token decryption, direct order submission |
| Market-data collector | Collect, validate, deduplicate, backfill, and store bars | Collector credential, data stores | Paper-execution credential, model promotion, trading-client code |
| Research agent | Propose bounded strategy specifications | Immutable datasets, approved feature/model catalog | Trading API, secrets, arbitrary production code execution |
| Trainer | Fit preprocessing, models, and calibrators | Development datasets and strategy specification | Holdout results before the candidate is frozen |
| Backtester | Simulate causal execution and produce evidence | Frozen dataset, fitted candidate, cost/risk assumptions | Broker APIs or mutable live state |
| Promotion controller | Apply confidence and safety rules | Backtest reports and immutable hashes | Order submission |
| Model registry | Record candidate, approved, active, and retired versions | Model metadata and artifact hashes | Changing model contents after approval |
| Live inference | Produce LONG, SHORT, or FLAT signals | Approved model and fresh market data | Broker credentials and mutable risk policy |
| Risk/execution worker | Size, validate, submit, and reconcile paper orders | Approved signals, current data, approved MVP paper credential | Research candidate generation or live-money endpoint |
| Audit service | Preserve append-only, hash-linked events and evidence | Decisions, jobs, orders, fills, failures | Secret values |

This separation prevents research-generated code and model logic from receiving broker authority. Multi-user identity and tenant isolation can be added later without changing that core boundary.

## Market-data system

### Do not scrape web pages

The "scraper" should be implemented as a **market-data collector** using Alpaca's official APIs:

- **Historical data:** REST requests for backfills and gap repair
- **Real-time data:** WebSocket subscriptions for one-minute bars in the MVP; trade/quote retention is a later optional enhancement
- **Broker/account state:** the approved Alpaca Paper Trading account, accessed only by the execution worker

This is faster and more reliable than HTML scraping and provides timestamps, schemas, rate-limit behavior, and official authentication.

### Why one central collector

A central collector prevents every model and backtest from repeatedly requesting the same market data. It also avoids interrupting collection while research is running. Alpaca notes that many data subscriptions allow only one active connection to a WebSocket endpoint, so the collector should normally run as a singleton and subscribe to the eight active tradable symbols plus SOXX, QQQ, and SPY through one connection. It must not subscribe to the future watchlist during the MVP. See Alpaca's [WebSocket market-data documentation](https://docs.alpaca.markets/us/docs/streaming-market-data).

The collector writes data; downstream systems read stored versions. Training and backtesting never depend on keeping an API request open.

### Hybrid storage: PostgreSQL plus Parquet

It is faster and cleaner to give the AI and backtester a dataset, but one storage format should not do every job.

**PostgreSQL is for:**

- recent and corrected bars;
- queryable metadata;
- the single MVP operator and approved paper-account metadata;
- research jobs and candidate status;
- promotion decisions;
- order intents, orders, fills, and audit events.

**Private object storage is for:**

- immutable Parquet market-data snapshots;
- feature matrices;
- fitted models and preprocessing pipelines;
- fold predictions and trade ledgers;
- equity curves and HTML reports;
- bootstrap and stress-test results.

For local development, object storage can be the filesystem or MinIO. In AWS it becomes private S3. Parquet is columnar and efficient for training; PostgreSQL is transactional and efficient for operational state.

### Canonical bar contract

Every stored bar should include:

- symbol and timeframe;
- bar interval-start timestamp in UTC;
- receipt timestamp in UTC;
- provider event timestamp in UTC when the raw event supplies one;
- open, high, low, close, volume;
- trade count and VWAP when available;
- provider and feed, such as Alpaca/IEX;
- adjustment mode;
- schema version;
- data-quality flags;
- correction status.

A useful unique identity is:

~~~text
(provider, feed, adjustment, symbol, timeframe, bar_timestamp_utc)
~~~

VWAP is required on the one-minute bars selected as execution references; a missing VWAP produces no simulated transition. It remains optional on other stored bar timeframes.

The collector must validate impossible OHLC values and negative volume, detect out-of-order messages, record reconnects, track a processing watermark, identify missing market-calendar intervals, and repair gaps with historical REST requests. An exact repeated payload is an ignorable duplicate; the same identity with changed values is a correction. Preserve the original raw event and correction history, then update the curated latest-value view deterministically.

### Dataset versioning

Every experiment must reference an immutable dataset manifest containing:

- dataset ID and SHA-256 content hash;
- universe version and the exact eight active tradable symbols;
- benchmark/context symbols and their non-tradable roles;
- inactive future-watchlist symbols and their exclusion role;
- time range and timeframe;
- provider, feed, and adjustment mode;
- schema and feature versions;
- missing-data and correction summary;
- creation timestamp;
- source-code Git commit.

A model is reproducible only if the exact data, features, code, parameters, and seed can be recovered. Provider, feed, timeframe, adjustment mode, and schema must match between training and live inference; a mismatch is a hard deployment failure, not a warning or fallback.

### Data licensing boundary

The private central store is platform input, not a public data product. Alpaca says its API data cannot be redistributed through another platform. Therefore the hosted website will not expose raw or reconstructable price series, downloadable bars, or a rebroadcast stream. Even derived charts and reports must be reviewed for licensing before the service is opened beyond a private classroom demonstration. See Alpaca's [redistribution guidance](https://alpaca.markets/support/redistribute-alpaca-api).

## What "training the AI" means

The project has two different forms of intelligence.

### 1. Research agent

The research agent generates ideas, but only inside a safe search space. It emits validated JSON or YAML—not arbitrary executable Python. A candidate specification contains:

~~~yaml
strategy_id: pooled_xgb_001
universe_version: nasdaq_semi_network_mvp_v1
search_space_id: pooled_xgb_search_v1
timeframe: 15Min
prediction_horizon_bars: 4

feature_bundle_id: G0_G1_G2_G3
feature_manifest_hash: sha256:...

model:
  family: xgboost_classifier
  max_depth: 4
  learning_rate: 0.03

edge_estimation:
  method: class_conditional_return

decision:
  actions: [LONG, SHORT, FLAT]
  minimum_class_probability: 0.60
  minimum_expected_edge_bps: 15

sizing:
  method: volatility_scaled

risk_policy_id: paper_v1  # injected and locked by the platform
cost_model_id: iex_bar_cost_v1
~~~

For the MVP, the strategy family, universe, horizon, execution semantics, and risk policy are fixed in advance. The research agent may vary only:

- one of the five feature bundles defined below;
- XGBoost maximum depth in `{2, 3, 4, 5}`;
- learning rate in `{0.01, 0.03, 0.05, 0.10}`;
- 100, 200, or 400 boosting rounds with inner-fold early stopping;
- `min_child_weight` in `{1, 5, 10}`, `subsample` in `{0.70, 1.00}`, and `colsample_bytree` in `{0.70, 1.00}`;
- `reg_alpha` in `{0, 0.1, 1.0}` and `reg_lambda` in `{1, 5, 10}`;
- class weighting in `{uniform, inverse_frequency_clipped}`, where the latter is normalized to mean 1.0 and clipped to `[0.5, 2.0]` using training-fold counts only;
- independent long and short probability thresholds in `{0.55, 0.60, 0.65, 0.70}`;
- minimum expected edge in `{10, 15, 20, 25}` basis points.

The versioned feature catalog is also finite:

| Group | Exact features and windows |
| --- | --- |
| `G0_identity_time` | symbol one-hot encoding, missingness indicators, minute-of-day sine/cosine, and day-of-week |
| `G1_price` | log returns over 1, 2, 4, and 8 bars; volatility over 8 and 20 bars; EMA ratios 8/26 and 12/26; RSI-14; ATR%-14; one-bar high-low range and close location; current-session opening gap versus prior regular-session close |
| `G2_volume` | volume and dollar-volume z-scores over 20 and 40 bars |
| `G3_cross_section` | active-basket relative returns over 1 and 4 bars and momentum ranks over 4 and 20 bars |
| `G4_market_context` | lagged SPY and QQQ returns over 1 and 4 bars and 20-bar volatility |

Every candidate contains `G0` and `G1`. Its bundle must be exactly one of `G0+G1`, `G0+G1+G2`, `G0+G1+G3`, `G0+G1+G2+G3`, or `G0+G1+G2+G3+G4`; arbitrary feature subsets and windows are invalid. Before search begins, the complete manifest, generator version, dependency lock, and seed `20260830` are frozen and SHA-256 hashed. A seeded discrete Latin-hypercube sampler produces 20 unique initial specifications. A revision may move only one registered threshold, regularization, depth, or boosting-round value by one grid step, switch the registered class-weight mode, or remove one optional feature group; it cannot introduce a new value or feature.

It may not change the eight-stock universe, context/benchmark roles, 15-minute frequency, four-bar horizon, target-position policy, XGBoost family, time splits, holdout, cost model, credentials, broker endpoints, paper-only enforcement, maximum risk, or promotion rules. The platform injects a fixed risk-policy ID after candidate validation. Hard symbol, theme-concentration, correlation, net, gross, loss, and drawdown ceilings live in that separate versioned policy; they are not AI-tunable fields. Candidates are compared at the same standardized risk budget so a weak idea cannot pass merely by reducing exposure.

The answer to "does the agent generate ideas?" is therefore **bounded yes**: it generates new candidate parameterizations inside a strategy template supplied by us. It does not invent arbitrary strategy families or executable code. Initial candidate generation uses the frozen seeded Latin-hypercube sampler. A local language model through Ollama may later translate evaluator feedback into one of the registered one-step revisions, but it receives the same limits and is not required for the research result. The predictive XGBoost model—not the language model—is the core trading intelligence.

### 2. Predictive model

The MVP uses one **pooled three-class XGBoost classifier** trained on listing-eligible observations across the eight-stock universe:

- **LONG**
- **SHORT**
- **FLAT**

A pooled model shares statistical information across the eight companies while retaining symbol identity and cross-sectional features. Same-timestamp observations are correlated, so every stock at a given timestamp must remain in the same temporal fold; they are not treated as eight independent market events.

The predeclared comparison set is:

1. equal-weight intraday-only portfolio of the eight MVP stocks under the same session/exposure rules — primary passive benchmark;
2. multinomial logistic regression with the same inputs — simple ML baseline;
3. fixed cross-sectional momentum — active rule baseline;
4. fixed cross-sectional mean reversion — active rule baseline;
5. always FLAT — safety baseline;
6. SOXX, QQQ, and SPY — contextual reporting only.

The logistic baseline uses the full approved feature bundle, L2 regularization with `C=1.0`, and fixed 0.60 probability/15-basis-point edge thresholds. The momentum baseline ranks 20-bar returns and targets the top two names long and bottom two short; the mean-reversion baseline reverses the ranking of four-bar basket-relative returns. Each selected rule-baseline position receives a fixed signed edge magnitude of 25 basis points before `paper_v1` volatility scaling; every unselected name receives zero. A rule baseline stays flat when fewer than six names are eligible or its top/bottom cutoff is tied. These choices, including tie-breaking by symbol, are frozen before candidate results exist. The XGBoost strategy must beat the best of all three active baselines after estimated costs. Accuracy by itself is not success.

### Frozen risk budget and passive benchmark

Every candidate and active baseline uses `paper_v1`, initialized with `$100,000` of simulated equity. For action sign `s_i` and expected edge `e_i`, the raw score is `s_i × min(abs(e_i) / 25 bps, 1) / max(sigma_i, 20%)`. `sigma_i` and the sample covariance matrix use the prior 20 complete regular sessions of 15-minute returns, annualized by `252 × 26`; a missing required history produces a zero target. Eigenvalues below `1e-8` are floored before portfolio volatility is calculated. One common multiplier scales the raw vector toward 10% forecast annualized volatility. The deterministic constraint order then clips and proportionally shrinks targets until all of these are true:

- absolute weight per symbol is at most 15%;
- gross exposure is at most 100% and net exposure remains between -30% and +30%;
- combined gross exposure is at most 60% for each frozen group: `{NVDA, AMD, SNDK, AXTI}` and `{CSCO, AAOI, HLIT, INSG}`;
- any connected correlation cluster whose prior-20-session pairwise-return correlation exceeds 0.80 has gross exposure at most 40%;
- cash cannot be borrowed, and short-sale proceeds cannot increase gross exposure above equity;
- target changes smaller than 0.25% of equity are ignored unless the required target is zero;
- a 2% session loss forces FLAT for the rest of that session; a 15% fold drawdown terminates that candidate for the rest of the fold.

Correlation clusters are recomputed once per session from data ending at the prior close. Connected components use an edge for an absolute pairwise correlation greater than 0.80; target rounding and any equal-priority tie use alphabetical symbol order. After each clip the remaining vector is rescaled downward only—never upward. In backtests, the session-loss state resets at the next session and a fold terminated by drawdown records zero exposure for its remaining dates; the next fold starts from its own `$100,000` initial equity. In shadow/paper operation, either breach instead disables new entries until operator review. The policy, formulas, constraint order, and rounding rules are versioned and hashed. Risk ceilings are identical for XGBoost, logistic regression, momentum, and mean reversion.

The primary passive benchmark is reproducible: in the same 9:47–9:48 a.m. ET execution window available to the first active decision, it enters equal long weights in every fold-eligible active name, applies one common scalar to satisfy the same 10% volatility target, 15% symbol cap, and 100% gross cap, then uses the common 3:44–3:45 p.m. forced-exit window. It rebalances every session, holds no overnight position, uses the same raw bars and base cost model, and receives no dividend because it is intraday-only. Before SNDK becomes eligible, weights are equal across the legitimately eligible names. Reports show both the full listing-aware sample and the common-eight period separately.

### Features

The v1 feature catalog and every permitted window are the frozen `G0`–`G4` table above. `G3` calculations use only legitimately eligible active names. `G4` uses lagged SPY and QQQ values only; SOXX remains benchmark-only and is excluded from model inputs. A feature outside that manifest causes schema validation to fail.

Every rolling feature at time t must use only information available at or before t. The exact same feature implementation must be shared by training, backtesting, shadow mode, and paper inference.

Cross-sectional features need an explicit decision watermark because live bars do not arrive simultaneously. For a 15-minute bar closing at boundary `C`, wait until exactly `decision_ready_at = C + 60 seconds`. Compute the basket only when every fold-eligible active symbol is present, or use the same recorded eight-stock availability-mask behavior used in training; otherwise skip the entire decision. The model result and durable order intent must be complete by `C + 120 seconds`, and the causal execution reference is the VWAP of the one-minute bar `[C + 2 minutes, C + 3 minutes)`. A missed deadline means no transition. SOXX, QQQ, SPY, and future-watchlist symbols never enter the basket. Never silently rank a different live basket than the model saw during training.

### Labels

For a four-bar horizon:

~~~text
15-minute bar(t) closes at boundary C
the basket is accepted at C + 60 seconds and the decision is final by C + 120 seconds
the label return begins at the VWAP of [C + 2 minutes, C + 3 minutes)
the forecast/label endpoint is the 15-minute close at C + 60 minutes
~~~

Use separate cost- and volatility-aware thresholds for the two directions:

~~~text
long_threshold(t) =
    max(estimated long round-trip cost,
        0.25 × expected volatility over the horizon)

short_threshold(t) =
    max(estimated short round-trip cost + conservative borrow proxy,
        0.25 × expected volatility over the horizon)

LONG  if forward return > long_threshold
SHORT if forward return < -short_threshold
FLAT  otherwise
~~~

`expected volatility` is the sample standard deviation of the prior 20 completed 15-minute log returns through `t`, multiplied by `sqrt(4)`. The causal label half-spread proxy is `max(10 bps, 10% × prior-bar range in bps)`. Label cost assumes 0.25% participation, giving 5 bps of impact per side under the frozen formula; the long round-trip estimate is `2 × (half-spread + 5 impact + 2 slippage + 1 fee-buffer)`. The short estimate adds the fixed 5-basis-point locate buffer and 25% annualized borrow prorated from the execution-window midpoint to the endpoint. Both entry and hypothetical endpoint use these decision-time proxies; labels never use future spread or volume. The observed forward return itself uses the causal execution-window VWAP and the later endpoint close.

This discourages the model from trading movements that are too small to overcome spread, slippage, turnover, and short costs. The label endpoint is not a mandatory simulated exit. Ordinary OHLCV data does not provide reliable point-in-time stock-loan availability, so backtests must disclose and stress a conservative borrow proxy. Live paper decisions separately require Alpaca's current shortable/easy-to-borrow eligibility.

The MVP uses a **target-position policy**, not overlapping four-bar lots. The model forecasts the return four 15-minute bars ahead, but every completed decision may replace the desired signed target. A change uses the later one-minute execution window defined above; a position persists until a later target transition or a risk rule forces it flat. Portfolio P&L comes from the filled signed position held between consecutive transition prices, with costs charged on every signed position delta—not from pretending every label is a separate trade.

The first model decision follows the 9:30–9:45 a.m. ET bar and can execute only in the 9:47–9:48 window. The last follows the 2:15–2:30 p.m. bar; its causal execution window is 2:32–2:33 and its four-bar label endpoint is 3:30 p.m. No model target is created after 2:30 p.m. The predeclared scheduler creates a zero target at 3:43 p.m., submits it by 3:44, and uses the 3:44–3:45 one-minute VWAP as the forced-exit reference. Every strategy and benchmark is therefore flat by 3:45 and overnight. The strategy contract fixes these rules so labels, turnover, trade counting, backtesting, benchmarks, shadow mode, and paper execution cannot interpret timing differently.

### Temporal training and validation

Never use a random train/test split for time-series trading data. Use purged walk-forward validation:

~~~text
Fold 1: [ expanding train ] [ purge ] [ validation ]
Fold 2:      [ expanding train ] [ purge ] [ validation ]
Fold 3:           [ expanding train ] [ purge ] [ validation ]
Fold 4:                [ expanding train ] [ purge ] [ validation ]
                                                           [ locked holdout ]
~~~

Predeclared MVP split rule:

- freeze on the most recent fully completed regular session chosen before search and record that date in the dataset manifest;
- reserve the final 63 trading sessions as the locked holdout;
- use the preceding 252 sessions as four consecutive 63-session outer validation folds;
- give Fold 1 all earlier valid history, requiring at least 378 training sessions, then expand training through each later fold;
- remove the four bars immediately before each validation/holdout boundary from fitting and embargo the four bars immediately after each evaluation boundary before those observations can enter later training;
- keep identical timestamp boundaries across symbols and never move a boundary after results are seen.

SNDK has materially shorter post-separation history than the other MVP names. A symbol is fold-eligible only if, before that fold's validation start, it has at least 60 completed regular sessions and at least 1,200 quality-approved 15-minute bars inside that fold's training data. Eligibility is frozen for the fold; live availability checks may make it temporarily FLAT but may not add a previously ineligible symbol. Never synthesize pre-listing observations, fill pre-spinoff intervals, or splice Western Digital history into SNDK. All stocks at one timestamp remain in the same fold, and cross-sectional ranks use only legitimately eligible members of the active eight-stock basket. Every report presents the listing-aware full sample and a separate common-eight sample beginning with the first fold in which all eight meet this rule.

Scalers, imputers, early stopping, return estimates, and probability calibration are learned entirely inside each outer training fold. The last 63 sessions of that training fold are the inner calibration/early-stopping slice, separated from the earlier fitting slice by a four-bar purge; early stopping patience is 25 rounds. Each outer validation window is causal model-selection evidence: it is untouched while its model is fitted, but its result can influence candidate ranking and bounded revisions, so it is not the final untouched test. The locked holdout is the only strictly untouched confirmation and is evaluated once after one candidate is selected and frozen. Repeatedly modifying a strategy against the same holdout would turn the holdout into training data.

The four outer-fold models are evaluation artifacts, not the deployable model. After one candidate specification wins development, the trainer fits exactly one **final candidate artifact** on all pre-holdout data: the final 63 pre-holdout sessions are again the calibration/early-stopping slice, a four-bar purge separates them from the earlier fit slice, and every parameter and seed stays frozen. The fitted preprocessing, XGBoost booster at its selected early-stopping iteration, temperature scaler, and class-conditional return table are serialized and hashed before the holdout is opened. The logistic baseline is fitted once with the identical pre-holdout/calibration boundary; rule and passive baselines have no fitted artifact. The holdout evaluates that exact hash, and the same exact XGBoost artifact—without post-holdout refitting—is the only artifact eligible for shadow or paper use.

### Probability calibration

The classifier produces a per-decision result such as:

~~~json
{
  "p_long": 0.68,
  "p_short": 0.09,
  "p_flat": 0.23,
  "expected_net_return_bps": 24
}
~~~

Probabilities are calibrated with one fixed multinomial temperature-scaling step on the inner 63-session calibration slice—not on the outer validation or locked holdout. L-BFGS-B optimizes `log(temperature)` within `[-5, 5]`, with maximum 1,000 iterations and function tolerance `1e-12`; the resulting positive temperature is stored with the model. Calibration method is not a candidate field. Expected net return is derived from the calibrated class probabilities and class-conditional forward returns estimated only from inner development data. A long or short signal is proposed only when its calibrated probability and estimated net edge pass the candidate's registered thresholds. Otherwise the action is FLAT.

## Deterministic backtesting

The backtester does not need to be AI. Its contract is:

~~~text
immutable dataset
+ fitted preprocessing pipeline
+ fitted model
+ candidate strategy specification
+ execution and risk assumptions
= reproducible evaluation report
~~~

It must simulate:

- signals only after the fixed watermark and fills only in the later one-minute execution window;
- market calendar, holidays, time zones, and daylight saving;
- bid/ask spread or a conservative approximation;
- slippage and transaction costs;
- documented short-eligibility and borrow-cost proxies, stressed conservatively;
- missing, stale, corrected, or halted data;
- a deterministic volume-participation cap and conservative full-fill/slippage model for research;
- maximum symbol, theme/correlation concentration, net, and gross exposure;
- position sizing and cash constraints;
- turnover and rebalancing;
- no unsupported extended-hours fills;
- no look-ahead bias.

### Frozen fill and cost model

`iex_bar_cost_v1` is fixed before search and applies to the ML strategy and every benchmark without requiring a multi-year quote archive:

1. The reference price for a target transition is the causal one-minute VWAP execution window defined above. A missing execution-minute VWAP means no research fill.
2. The causal half-spread proxy is the greater of 10 basis points and 10% of the prior completed 15-minute high-low range, measured in basis points. Because it uses only the prior completed bar, it is available identically in training, backtesting, and live inference.
3. Proposed delta shares are clipped to 1% of the prior completed 15-minute bar's volume. Zero or missing prior volume means no fill.
4. Market-impact slippage is `10 × sqrt(participation / 0.01)` basis points, plus a fixed 2-basis-point slippage buffer and 1-basis-point fee/regulatory buffer per side.
5. A buy fills at `execution_vwap × (1 + adverse_bps / 10,000)` and a sell at `execution_vwap × (1 - adverse_bps / 10,000)`, where `adverse_bps = half_spread + impact + 2 + 1`.
6. Short exposure pays a 25% annualized borrow proxy prorated by actual holding seconds, plus a 5-basis-point locate/availability buffer whenever a new short episode opens. A live paper short still requires current broker eligibility.

Fifteen-minute data cannot reproduce exchange queue position or credible partial fills. The research model therefore assumes a conservative full fill only after the causal participation clip; actual partial fills are handled and audited during Alpaca paper reconciliation. The 1.5× and 2× stress tests multiply half-spread, impact, fixed slippage, fee buffer, locate buffer, and borrow charge by the stated factor while leaving market returns and the 1% participation cap unchanged. Every report shows base, 1.5×, and 2× results.

IEX quote-derived spread summaries may be studied later, but they are not an MVP dependency and cannot replace this frozen proxy inside the current dataset/holdout generation.

### Position episodes and trade counting

A completed round trip is one per-symbol signed position episode. An episode opens when a filled target moves from zero to nonzero and closes when it next reaches zero. A partial increase or reduction changes P&L and costs but does not create another round trip. A direct long-to-short reversal closes one long episode and opens one short episode at the same transition; the new short counts only when it later closes. The scheduled 3:44–3:45 p.m. flatten closes every open episode, and no episode can cross a session, fold boundary, or holdout boundary. The 200-total and 50-per-side gates count these completed episodes, not decisions, fills, or individual bar returns.

### Evaluation metrics

**ML diagnostics:**

- balanced accuracy;
- log loss and Brier score;
- calibration curve;
- long/short precision and recall;
- information coefficient.

**Trading evidence:**

- net return;
- Sharpe, Sortino, and Calmar ratios;
- maximum drawdown;
- profit factor;
- trade count and turnover;
- win rate and average win/loss;
- gross and net exposure;
- long-side and short-side performance;
- per-symbol and per-fold performance;
- P&L concentration;
- cost-stress performance.

A classifier can have strong ML metrics and still be an unusable trading strategy. Promotion is based on trading evidence and hard safety rules, not one accuracy number.

## The confidence and revision loop

There are two confidence values and they must not be confused.

### Signal confidence

Signal confidence is the calibrated model probability for one decision. It answers:

> Is the model confident enough to propose this specific LONG or SHORT signal?

It controls entry into the deterministic risk engine. It does not approve the strategy itself and does not bypass position limits.

### Strategy confidence

Strategy confidence evaluates the entire candidate on the stitched daily portfolio returns produced by the outer walk-forward validation folds. These are causal development/model-selection results, not the untouched final holdout. Use exactly 10,000 moving-block-bootstrap resamples with seed `20260830`, circular blocks, and block length `max(5, round(number_of_days^(1/3)))`. Annualize Sharpe with 252 trading days.

The primary statistical measure is:

~~~text
strategy_confidence =
    bootstrap support that out-of-sample net Sharpe > 0.50
~~~

The predeclared MVP promotion policy requires all of the following:

- bootstrap support at least 95%, conditional on the selected model and assumptions;
- point-estimate net Sharpe at least 0.75;
- maximum drawdown no greater than 15%;
- positive after-cost return in at least three of four folds;
- at least 200 completed simulated round trips, not per-bar signals;
- total net return greater than zero and net Sharpe greater than zero under 2× cost assumptions;
- net Sharpe no lower than the equal-weight eight-stock passive benchmark over the same evaluation timestamps;
- net Sharpe at least 0.10 above the best of logistic regression, fixed momentum, and fixed mean reversion under the same risk and costs;
- net return and net Sharpe both greater than zero on the separately reported common-eight period;
- no single symbol contributing more than 25% of gross absolute P&L contribution;
- both the long-only and short-only attribution slices non-negative after base costs, with at least 50 completed round trips on each side;
- passage of the predeclared block-bootstrap/Holm development screen below;
- all data-quality, leakage, reproducibility, and safety checks passing.

These are project defaults, not claims that a strategy is safe or profitable. Bootstrap support is conditional evidence under this dataset and model-selection process; it is not the probability of future profitability. A dashboard may also display a weighted 0–100 readiness score, but that score must be labeled as a rubric—not as a probability.

### Exact multiple-testing development screen

For each initial or revised candidate, mean-center its stitched daily returns to impose the zero-mean null, generate 10,000 circular moving-block resamples with the same block-length rule, and compute

~~~text
p_i = (1 + count(null_bootstrap_sharpe >= observed_sharpe)) / 10,001
~~~

The seed for a candidate is the first 32 bits of `SHA-256("20260830" + candidate_id)`. Apply the Holm step-down thresholds at `alpha = 0.05` across **every** candidate in the closed trial ledger, with no reduction for correlation between trials. The evaluator is frozen as `block_holm_screen_v1`, unit-tested against fixed examples, and hashed before search.

Because the two revision paths are chosen adaptively from development results, this is explicitly a conservative **development screening rubric**, not a formal claim of 5% family-wise error control. Its purpose is to penalize repeated search; the one untouched holdout remains the confirmatory evidence. The method, alpha, block rule, trial count, or candidate family cannot be changed after results are seen.

### Locked-holdout rule

After the development budget is closed, exactly one frozen nominee receives the holdout. It must pass every development rule above and is selected without holdout information by: highest lower 95% moving-block-bootstrap bound for development net Sharpe, then highest worst-fold net Sharpe, then lowest turnover, then lexicographically smallest candidate ID.

`HOLDOUT_PASSED` requires all of the following on the predeclared 63-session holdout, with no refitting or threshold change:

- base-cost total net return greater than zero and annualized net Sharpe at least 0.50;
- maximum drawdown no greater than 15%;
- at least 50 completed round trips, including at least 10 long and 10 short episodes;
- total net return greater than zero under 1.5× costs;
- net Sharpe no lower than both the primary passive benchmark and the best active baseline on identical holdout timestamps;
- every data-quality, leakage, artifact-hash, risk, and paper-safety check passing.

No bootstrap probability or parameter optimization is performed on the 63-session holdout. A failure produces only `REJECTED`, consumes that holdout generation, and cannot trigger a revision.

### Promotion states

~~~text
CANDIDATE
  → DEVELOPMENT_PASSED
  → HOLDOUT_PASSED
  → SHADOW_PAPER
  → PAPER_ACTIVE
  → RETIRED
~~~

A candidate is never sent directly from training to paper orders. It first runs in shadow mode, where real-time signals are recorded but not submitted. The initial rule is at least 10 completed trading days and at least 50 eligible signals, plus zero unresolved data, risk, deployment, or reconciliation failures. Both the time and signal requirements must pass before paper activation.

### Structured evaluator feedback

When a candidate fails, the backtester and promotion controller return machine-readable feedback:

~~~json
{
  "candidate_id": "pooled_xgb_017",
  "decision": "REVISE",
  "failure_codes": [
    "EXCESSIVE_TURNOVER",
    "COST_SENSITIVE",
    "UNSTABLE_ACROSS_FOLDS"
  ],
  "allowed_changes": [
    "increase_no_trade_threshold",
    "reduce_model_complexity",
    "increase_regularization"
  ]
}
~~~

Examples:

| Failure | Allowed response |
| --- | --- |
| Excessive turnover | Widen the no-trade zone within its registered range |
| Cost sensitive | Require a larger predicted edge |
| High drawdown | Reject unstable features or simplify the strategy; do not change the standardized comparison risk |
| Poor calibration | Increase regularization or move a probability threshold one registered grid step; calibration method stays fixed |
| Unstable folds | Simplify the model |
| Symbol concentration | Regularize or remove one optional feature group; hard risk caps remain fixed |
| Weak short side | Switch the registered class-weight mode, move the short threshold one grid step, or reject the candidate |
| Insufficient trades | Reject this generation; only genuinely new elapsed data may create a later dataset version |

### Bounded autonomy

To avoid an infinite search that eventually overfits by chance:

- generate exactly 20 unique initial XGBoost candidates for each dataset version and holdout generation—not per API job;
- rank the initial 20 by fewest failed development rules, then highest bootstrap strategy confidence, highest median-fold net Sharpe, highest worst-fold net Sharpe, lowest turnover, and lexicographically smallest candidate ID;
- allow only the top two under that exact ordering to receive at most two registered one-step revisions each;
- record every attempted candidate in one global trial ledger;
- mark a holdout generation consumed after its one permitted evaluation;
- close the development ledger before applying `block_holm_screen_v1` and choosing the single holdout nominee;
- require immutable dataset and candidate versions;
- use deterministic seeds;
- require comparison with simple baselines.

If no candidate passes every development gate, the family is rejected without opening the holdout. If the one holdout nominee fails, return only **REJECTED** and reject that candidate family. Detailed holdout results may be archived for the final report but must never feed the revision loop. A new API job cannot reset the search budget or unlock a consumed holdout.

## What a promoted model needs to execute

A model file alone cannot trade. Paper execution requires all of the following:

1. an approved immutable model and preprocessing pipeline;
2. the exact feature and strategy specification;
3. a matching model-registry hash;
4. fresh, validated real-time data;
5. the explicitly authorized MVP Alpaca paper account;
6. current account, position, and open-order state;
7. a deterministic risk policy;
8. an open supported market session;
9. a unique order intent and client order ID;
10. no unresolved discrepancy from any prior order or reconciliation cycle.

If any item is missing or inconsistent, the result is FLAT/no trade.

Every newly submitted order must then reconcile against Alpaca's order/trade updates. A timeout, mismatch, or ambiguous state halts subsequent submissions until the operator resolves it.

Research-generated artifacts must use a safe, allowlisted execution format such as XGBoost JSON/UBJ or ONNX. The inference service verifies the approved hash, schema, library compatibility, and model version before parsing. Pickle and joblib artifacts are prohibited in inference and execution because loading them can run arbitrary code.

## Why risk and execution still need market data

Alpaca handles the paper account, accepts orders, and simulates fills, but it does not decide whether this platform should place an order. The platform still needs current market data to:

- calculate live model features;
- reject stale signals;
- estimate order value and position size;
- check spread and liquidity;
- apply volatility and exposure limits;
- compare expected edge with estimated cost;
- reconcile the decision price with the eventual simulated fill.

The risk engine is deliberately independent from the AI. It validates model approval, market freshness, shortability, account buying power, per-symbol size, gross and net exposure, daily loss, drawdown, duplicate intent, market hours, and kill-switch state.

The executor then:

1. stores a durable order intent;
2. assigns a unique client order ID;
3. calls only the Alpaca paper endpoint;
4. records the response;
5. consumes trade updates;
6. reconciles local state with Alpaca;
7. halts on ambiguous state instead of blindly retrying.

This produces effective-once behavior even though networks do not guarantee perfect exactly-once delivery.

## MVP credentials and future account model

The system uses two distinct Alpaca account/credential pairs. Separation is enforced by which process receives each secret, collector code that contains no trading client, and a trading-hostname application denylist. The design does not claim that Alpaca gives an ordinary API key a special read-only market-data scope or that ordinary IP-based Kubernetes NetworkPolicy can reliably distinguish changing provider hostnames.

### Collector credential and operational account

- Used only by the singleton collector
- Reads historical and real-time data
- Writes the private central store
- Belongs to a separate operational Alpaca account from the approved paper-execution account
- Is never loaded by code containing a trading client; the collector rejects trading hostnames, with an allowlisting egress proxy or FQDN-aware Cilium policy available as cloud hardening
- May technically possess broader provider capability; its effective data-only role is an application/infrastructure control, not a provider-scope claim
- Never appears in the browser or research worker

### MVP paper authorization

The semester MVP uses one manually provisioned Alpaca paper key from the approved execution account. It is distinct from the collector pair and is mounted only into the paper-execution worker, stored locally in an ignored environment file and later in AWS Secrets Manager. It never passes through the browser, YAML, GitHub, logs, screenshots, or model prompts.

### Future multi-user Alpaca authorization

For a multi-user version, use Alpaca OAuth:

1. the authenticated user selects **Connect Alpaca Paper Account**;
2. the backend generates a cryptographically random state value bound to that user and browser session, with a short expiration and one-time use;
3. the user is redirected to Alpaca with **env=paper**, the minimum **trading** scope, and one exact allowlisted redirect URI;
4. Alpaca redirects an authorization code to that callback;
5. the backend verifies the state, user/session binding, expiration, one-time status, and callback before exchanging the code server-side;
6. the encrypted token is stored and never returned to the browser;
7. only the paper-execution worker may decrypt it.

Alpaca documents third-party OAuth, the paper environment parameter, backend token exchange, and the paper order endpoint in its [OAuth Trading API guide](https://docs.alpaca.markets/us/docs/using-oauth2-and-trading-api).

OAuth application registration and the rest of this flow are product-extension work, not requirements for the research result.

Disconnect is also a controlled workflow: disable new execution first, reconcile outstanding paper orders and positions, invalidate/delete the stored connection, and append an audit event. A missing or revoked token leaves execution disabled.

### Redundant paper-only enforcement

- OAuth authorization explicitly selects paper.
- The executor hard-codes the paper trading hostname.
- The live-money hostname is not configurable.
- Startup verifies paper-account authority and fails closed if the account/endpoint is not paper; tests also reject live hostnames and any attempt to make paper mode configurable.
- CI fails if a live endpoint is introduced.
- Submission is disabled by default.
- The MVP operator has a kill switch; a future multi-user system gives each account its own switch.
- An uncertain broker state halts new orders.
- The research agent and inference worker have no Alpaca credential or trading client; an allowlisting egress proxy/FQDN-aware policy is optional defense in depth.

## APIs and subsystem communication

Use the following communication patterns:

| Path | Technology | Reason |
| --- | --- | --- |
| Browser → control API | HTTPS REST/JSON; local operator auth for MVP, Cognito JWT later | User-facing control requests |
| Alpaca → collector | WebSocket | Continuous real-time market events |
| Collector → Alpaca historical | HTTPS REST | Backfills and gap repair |
| API → workers | PostgreSQL job table + transactional outbox for MVP; SQS only in the optional AWS showcase | Training/backtests can outlive an HTTP request without making a queue a research dependency |
| Workers → PostgreSQL | SQLAlchemy over TLS | Transactional state and searchable metadata |
| Workers → object storage | S3 API | Large immutable datasets, models, and reports |
| Executor → Alpaca paper | HTTPS REST + trade-update WebSocket | Paper orders and reconciliation |
| Internal events | Versioned Pydantic schemas | Accurate, validated contracts |

The versioned job/event envelope stored with a job should include:

~~~json
{
  "schema_version": 1,
  "event_id": "evt_...",
  "correlation_id": "job_...",
  "operator_id": "op_mvp",
  "paper_account_id": null,
  "created_at": "2026-08-30T14:00:00Z",
  "idempotency_key": "...",
  "payload_reference": "s3://private-bucket/..."
}
~~~

Large datasets never travel inside messages. Research envelopes keep `paper_account_id` null. A later shadow/paper activation envelope may name `paper_mvp_001`, but all message fields remain routing hints rather than authorization. A worker uses the opaque job ID to re-read the MVP operator, approved model, artifact location, and current permission state from authoritative PostgreSQL records, then verifies the artifact checksum. The execution worker separately re-reads the sole approved paper-account record and confirms any named ID matches it. It never trusts an operator ID, account ID, or object URI from a message by itself. Tenant and broker-connection reauthorization are future multi-user requirements, not claims about this MVP.

Candidate control endpoints may look like:

~~~text
POST /v1/research-jobs
GET  /v1/research-jobs/{job_id}
GET  /v1/strategies
GET  /v1/strategies/{strategy_id}/report
POST /v1/strategies/{strategy_id}/shadow
POST /v1/strategies/{strategy_id}/activate-paper
GET  /v1/paper/positions
GET  /v1/paper/orders
POST /v1/paper/kill-switch
GET  /v1/system/status
~~~

The MVP API is private and single-operator. If Cognito is added later, the API must verify the JWT signature, issuer, audience/client ID, expiration, and token type, then derive the tenant identity from the verified subject claim. It never trusts a user ID supplied only in the request body. Candidate-budget, concurrency, and request-size limits apply even in the one-user deployment.

### Accuracy and delivery controls

- Strict schema validation and versioning
- PostgreSQL transactions
- Transactional outbox for atomic job creation and event publication; the optional SQS adapter drains it
- Idempotency keys and uniqueness constraints
- Explicit `FAILED` and `DEAD` job states after bounded retries; a dead-letter queue is added only with the optional SQS adapter
- Timeouts and bounded retries
- Content hashes and append-only/tamper-evident artifact metadata
- Atomic model promotion
- Order reconciliation before retry
- Correlation IDs from user request through model, decision, and order
- UTC timestamps everywhere

## Recommended technology stack

| Area | Choice |
| --- | --- |
| Primary language | Python 3.11+ |
| Data and ML | pandas or Polars, NumPy, scikit-learn, XGBoost |
| API | FastAPI, Pydantic, SQLAlchemy |
| Historical/real-time provider | Alpaca REST and WebSocket through alpaca-py |
| Backtesting | Extend the repository's deterministic engine |
| Operational database | PostgreSQL |
| Local lightweight state | SQLite for tests/replay only |
| Research files | PyArrow/Parquet |
| Object storage | Local filesystem/MinIO, then private S3 |
| Job dispatch | PostgreSQL job/outbox + in-process or dedicated worker for MVP; SQS only for the optional AWS showcase |
| Model tracking | PostgreSQL registry + S3 artifacts; MLflow is optional |
| Frontend | Keep Streamlit for the MVP; React/Next.js is a stretch goal |
| Authentication | Private/local operator access for MVP; Amazon Cognito only for a future multi-user deployment |
| Secrets | Local ignored environment file, then AWS Secrets Manager |
| Containers | Docker and Docker Compose |
| Kubernetes learning | k3d or kind locally; k3s on one EC2 instance |
| Infrastructure | Terraform and Helm/Kubernetes manifests |
| CI/CD | GitHub Actions with GitHub OIDC to AWS |
| Monitoring | Structured logs, Prometheus/Grafana locally, CloudWatch in AWS |
| Testing | pytest, Ruff, mypy, coverage, replay fixtures |
| Security scanning | Gitleaks, pip-audit, Trivy, Dependabot |

For a semester project, keep the number of deployable workloads small but preserve the credential boundary:

1. **control-api**
2. **market-data-worker**
3. **research-worker** containing generation, training, and backtesting
4. **inference-worker** containing approved-model feature calculation and signals, with no broker token
5. **paper-execution-worker** containing deterministic risk, execution, and reconciliation
6. **frontend**

The registry and promotion gate are logical components backed by PostgreSQL/S3; they do not need separate containers.

## Security model

### Future multi-user isolation

This subsection applies only if the product-extension tier is attempted. The core MVP has one operator and one paper account.

Every user-owned table contains a user or tenant ID, including:

- users;
- broker connections;
- research jobs;
- candidate strategies;
- backtest runs;
- paper accounts;
- order intents;
- orders and fills;
- audit events.

Enforce ownership in application queries and, when PostgreSQL is introduced, with Row-Level Security. Use tenant-prefixed S3 object paths. Generate short-lived download URLs only after verifying ownership.

### Least privilege

- The frontend receives no infrastructure or broker credentials.
- The API can create jobs but cannot place orders.
- The collector receives only the distinct collector credential and never the paper-execution credential.
- The research worker can read datasets but cannot reach trading APIs.
- The inference worker can read approved safe-format models but has no broker-token mount or trading-network permission.
- The paper executor receives validated signals and may decrypt broker tokens. A shared executor role can technically reach the configured secret namespace, so it must re-authorize every connection from the database and never select a secret directly from message input. Strong per-user cryptographic isolation would require per-user tasks/roles or scoped encryption and remains a platform stretch goal.
- GitHub Actions assumes a short-lived AWS role through OIDC; no permanent AWS access key is stored in GitHub.
- Kubernetes workloads use separate service accounts and IAM roles where supported.

### Infrastructure controls

- TLS for every public and cloud connection
- Private database and object storage
- S3 Block Public Access, encryption, and versioning
- No public SSH; use AWS Systems Manager
- Default-deny Kubernetes NetworkPolicies
- Kubernetes RBAC and namespace separation
- Non-root containers
- Read-only root filesystems where practical
- Dropped Linux capabilities and RuntimeDefault seccomp
- Resource limits and health probes
- Secret and authorization-header redaction
- CloudTrail/audit logging
- Encrypted backups and tested restoration
- Budget alarms before any deployment

Kubernetes Secrets are not a sufficient long-term secret store by themselves. Valuable credentials should remain in an external encrypted store and be mounted only into the authorized worker. Application records and ordinary versioned S3 objects are not legally immutable or WORM: use append-only keys, hashes/hash chains, separated writer permissions, and audit logs to make the semester evidence tamper-evident. If true retention is required later, add S3 Object Lock and a separately protected approval/signing key.

## Docker and Kubernetes plan

Docker creates a reproducible image for each workload. Docker Compose runs the full system locally. Kubernetes then schedules those images, restarts failed workloads, injects configuration, applies network policies, and manages controlled rollouts.

### Local development

~~~text
Docker Compose
  ├── frontend
  ├── control-api
  ├── market-data-worker
  ├── research-worker
  ├── inference-worker
  ├── paper-execution-worker
  ├── PostgreSQL
  └── MinIO/local object storage
~~~

Use k3d or kind to practice:

- Deployments and Jobs
- Services and Ingress
- ConfigMaps
- external secret injection
- persistent volumes
- NetworkPolicies
- service accounts and RBAC
- liveness/readiness probes
- rolling updates and rollback
- Helm releases

Training and backtests should be Kubernetes Jobs, not permanently running services. The singleton data collector should use one replica with a recreate strategy or leader election. Reproducible images also require a dependency lockfile inside the build, a base image pinned by digest, deterministic build inputs, and an image digest recorded with each deployment.

## AWS deployment and the meaning of "free"

A permanently online application with AWS, Kubernetes, a database, secure secrets, storage, and monitoring cannot honestly be guaranteed to cost $0 forever. Free-tier eligibility and service pricing change. There are two responsible targets.

### Guaranteed $0 development environment

- Run Docker Compose locally
- Run k3d/kind locally
- Use local PostgreSQL
- Use local files or MinIO
- Use the PostgreSQL job table/outbox with an in-process or dedicated local worker
- Use Alpaca paper credentials only
- Use GitHub for source and CI within the account's included allowance

This still demonstrates Docker and Kubernetes.

### Low-cost semester cloud demo

- One HTTPS origin through Caddy/NGINX ingress on the small instance; a static S3/CloudFront frontend is optional later
- One small EC2 instance
- k3s on that instance
- Encrypted EBS storage
- PostgreSQL on the instance for the semester MVP
- Private S3 for datasets, models, reports, and backups
- PostgreSQL-backed jobs for the baseline demo; SQS may be added only to demonstrate the optional queue adapter
- ECR for images
- Secrets Manager for platform and broker credentials
- CloudWatch and CloudTrail
- Terraform to create and remove the environment

Only the HTTPS ingress is inbound internet-facing. Use DNS-validated TLS so only port 443 must be public; keep the Kubernetes API, NodePorts, PostgreSQL, object storage, dashboards, and metrics private. The collector and executor still need controlled outbound HTTPS/WebSocket access to Alpaca and AWS services. Do not expose SSH; administer the instance through Systems Manager.

Single-node k3s does not provide EKS Pod Identity/IRSA and all pods share one host kernel and potentially one EC2 role. The semester k3s deployment is therefore one shared AWS trust boundary, not strong multi-tenant isolation. Keep the EC2 role minimal and do not claim that Kubernetes service accounts create separate AWS permissions. Limit hosted k3s paper execution to the explicitly approved single-user demo. A future multi-user executor needs separately scoped compute and credentials, such as ECS Fargate task roles or EKS with Pod Identity, while the research worker remains unprivileged.

Use AWS Academy, student credits, or current Free Tier credits if eligible. Configure budget alerts at very low thresholds before creating resources. Stop nonessential compute outside demonstrations and destroy the environment after the course.

Before deployment, create a current calculator estimate covering EC2 runtime, the public IPv4 address, EBS and snapshots, S3 storage/requests/transfer, ECR storage, optional SQS requests, CloudWatch logs, DNS/domain charges, backups, and Secrets Manager entries for the separate collector and paper-execution credential pairs. Add Cognito only if the future multi-user extension is actually attempted. This design intentionally avoids an always-on NAT Gateway and Application Load Balancer, both of which would undermine the low-cost goal.

Do not use EKS for the always-on semester deployment. AWS currently charges $0.10 per cluster-hour for a standard-support EKS control plane before worker nodes and other resources—roughly $73 per 730-hour month. See the official [Amazon EKS pricing page](https://aws.amazon.com/eks/pricing/) and verify current pricing before deployment. Run Kubernetes locally and use single-node k3s on EC2 for the cloud demonstration.

The [AWS Free Tier page](https://aws.amazon.com/free/) is the source of truth for current credit and eligibility rules. "Using credits" means the bill may be covered; it does not mean the architecture has no cost.

## CI/CD plan

Every pull request should:

1. format and lint;
2. type-check;
3. run offline unit and integration tests;
4. run look-ahead/leakage tests;
5. run paper-only endpoint safety tests;
6. scan dependencies;
7. scan commits for secrets;
8. build and scan Docker images.

Merges to the deployment branch should:

1. build immutable image tags using the Git commit SHA;
2. push images to ECR;
3. apply Terraform and Helm through a short-lived GitHub OIDC role;
4. run database migrations;
5. deploy with health checks;
6. record the deployed commit and model hashes;
7. support rollback to the previous image and model.

CI must never connect to Alpaca or submit even a paper order. Use fakes, replay, observer mode, and dry-run paths.

## Repository strategy

Do **not** clear the repository and start over. The existing implementation already demonstrates causal backtesting, replay, risk controls, persistence, reconciliation, receipts, observer mode, paper-only boundaries, a dashboard, and tests. Deleting it would remove useful work and make the project less credible.

Evolve it in phases:

- preserve the existing prototype with a Git tag;
- work on a feature branch;
- keep tests passing during migration;
- extract repository/storage interfaces first, because current persistence, reporting, observer evidence, and dashboard paths include SQLite-specific queries; then migrate their consumers to PostgreSQL/S3;
- add timeframe and correction lineage to the bar contract and database identity before introducing intraday storage;
- split configuration into exactly eight active `tradable_symbols`, read-only `context_symbols`, a primary benchmark, and an inactive `future_watchlist`; enforce that future symbols cannot enter feature, model, signal, or order contracts;
- migrate all signed-position assumptions—not only the backtester—including strategy/allocator contracts, weight validation, risk, order planning, asset checks, fake broker, reconciliation, reporting, receipts, and safety tests;
- replace the current once-per-session/prior-daily-bar schedule with causal minute-to-15-minute aggregation, an intraday scheduler, durable decision watermarks, restart recovery, and duplicate-decision prevention;
- make Docker builds consume the checked-in lockfile, pin the base image by digest, and record image digests instead of installing open dependency ranges;
- add the training and promotion loop before multi-user cloud features;
- keep every README status statement honest about what is implemented.

A future repository layout may become:

~~~text
apps/
  api/
  frontend/
services/
  market_data/
  research/
  paper_trading/
packages/
  contracts/
  features/
  backtest/
  risk/
configs/
infra/
  docker/
  kubernetes/
  terraform/
tests/
docs/
~~~

This is a target organization, not a requirement to move all files immediately.

## Semester roadmap

| Weeks | Milestone | Required result |
| --- | --- | --- |
| 1 | Baseline and delivery rails | Tag the prototype, freeze safety rules, verify offline tests, make CI run, lock container dependencies, and keep a working Compose path from the start |
| 2–3 | Eight-stock data foundation | Extract storage interfaces; validate and ingest NVDA, AMD, CSCO, SNDK, AAOI, AXTI, HLIT, and INSG plus read-only context; store the 18 future symbols only as inactive metadata; freeze versioned Parquet snapshots |
| 4 | Real-time intraday pipeline | Add minute-to-15-minute aggregation, bounded basket watermark, reconnect/gap repair, durable scheduling, restart recovery, and duplicate-decision prevention |
| 5 | Signed-position migration | Update contracts, allocator, backtester, risk, planning, broker fakes, reconciliation, reporting, and tests for long/short/flat behavior |
| 6 | Feature pipeline and baselines | Build shared features and compare logistic, momentum, mean-reversion, flat, and benchmark baselines |
| 7 | Eight-stock pooled ML model | Train the fixed-family LONG/SHORT/FLAT XGBoost model with deterministic artifacts |
| 8 | Walk-forward evaluation | Add nested chronological calibration, purge/embargo, fold reports, listing-aware eligibility, and a locked holdout |
| 9 | Confidence gate | Add block bootstrap, hard promotion rules, model registry, and structured failure codes |
| 10 | Bounded research loop | Enforce 20 initial candidates, at most two revisions for each of the best two, one global trial ledger, one-use holdout, and machine-readable rejection feedback |
| 11 | Shadow system | If a model passes, run that exact safe-format artifact on fresh data without orders; otherwise demonstrate that `REJECTED` artifacts cannot activate; preserve the inference/execution boundary and receipts |
| 12 | Paper/demo integration | Complete and dry-run the paper-only safety path, idempotency, kill switch, reconciliation, Streamlit, private FastAPI job/report API, and Compose; submit a paper order only if an approved model exists |
| 13 | Integration buffer and Kubernetes | Fix end-to-end failures, preserve evidence, and run the same images and Jobs in k3d/kind with security policies |
| 14 | Final report and optional AWS showcase | Complete the report/demo first; deploy temporary Terraform-managed k3s only if the core and paper/demo tiers are already stable |

### Priority order if time becomes limited

1. Reliable historical data and immutable datasets
2. Causal long/short backtester
3. One pooled XGBoost model for the eight active stocks
4. Walk-forward confidence gate
5. Structured revision loop
6. Shadow mode
7. One-user Alpaca paper execution
8. Tested CI and Docker Compose
9. Local Kubernetes
10. Temporary AWS deployment
11. Cognito, multi-user OAuth, and React as stretch goals

A complete, reproducible one-user system is a stronger semester project than an unfinished collection of microservices.

## Required artifacts for every candidate

- Dataset manifest and content hash
- Universe version, timestamp range, and every symbol's active/context/benchmark/future role
- Feature specification
- Candidate strategy JSON/YAML
- Fitted preprocessing pipeline
- Fitted model in an allowlisted non-pickle format
- Fold-level predictions
- Trade ledger
- Equity curve
- ML and trading metrics
- Bootstrap confidence report
- Cost and parameter stress reports
- Promotion or rejection decision
- Git commit SHA
- Model, evaluator, and risk-policy versions

Large immutable artifacts live in object storage; searchable metadata and lifecycle state live in PostgreSQL. Only the broker-free inference worker verifies and loads the safe-format model artifact after its hash matches an approved registry record. The paper executor never deserializes a model; it verifies that each incoming signal cites the currently approved model ID and hash.

## Current prototype: installation and safe commands

Python 3.11 or newer is required.

~~~bash
git clone https://github.com/yudhiishvs/autonomous-quant-agent.git
cd autonomous-quant-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,dashboard]"
~~~

The existing offline tests, synthetic backtest, and deterministic replay require no Alpaca credentials or internet access. These checked-in commands still exercise the legacy ETF-based prototype configurations; they do not yet implement or prove the planned eight-stock MVP described above.

~~~bash
ruff format --check .
ruff check .
mypy src
pytest -q
python -m adaptive_trader.cli backtest --config configs/backtest.yaml --synthetic
python -m adaptive_trader.cli replay --config configs/replay.yaml
~~~

The current read-only observer and dry-run paths are:

~~~bash
python -m adaptive_trader.cli doctor --config configs/observer.yaml
python -m adaptive_trader.cli observe --config configs/observer.yaml
python -m adaptive_trader.cli paper-once --config configs/observer.yaml --dry-run
~~~

Do not enable paper submission merely because the code contains a paper executor. Follow the repository's existing readiness gates and runbook. Development and CI must never submit orders.

See the existing [architecture](docs/architecture.md), [methodology](docs/methodology.md), [data dictionary](docs/data_dictionary.md), [paper runbook](docs/live_paper_runbook.md), and [incident-response guide](docs/incident_response.md).

## Acceptance tiers

### Core research MVP — required

- Exactly NVDA, AMD, CSCO, SNDK, AAOI, AXTI, HLIT, and INSG are validated as the active Nasdaq-listed tradable universe.
- Automated tests prove that future-watchlist and context symbols cannot be prediction targets, portfolio positions, order-intent symbols, or broker orders. Lagged QQQ/SPY values may influence the eight active symbols only through their explicitly approved context features.
- Historical and real-time bars share one canonical schema.
- Collection restarts without silently losing or duplicating bars.
- A frozen Parquet dataset can reproduce a training run.
- Features and labels pass no-look-ahead tests.
- One pooled model for the eight active stocks produces LONG, SHORT, or FLAT probabilities.
- Reports compare it with the equal-weight eight-stock passive benchmark, logistic regression, fixed momentum, fixed mean reversion, and always FLAT under predeclared rules.
- The deterministic backtester includes costs and short assumptions.
- Walk-forward results and a locked holdout are preserved.
- The strategy-confidence gate produces either a fully evidenced `REJECTED` conclusion or one immutable, holdout-passed model.
- The revision loop is bounded and records every attempt.
- The final report treats either measured outperformance or a reproducible negative result as a completed research outcome.

### Paper/demo MVP — target

- The risk engine blocks stale, oversized, duplicate, ineligible, or unauthorized decisions.
- If and only if a model passes both gates, it first runs in shadow and the one explicitly authorized MVP operator may activate it against Alpaca paper only.
- If every candidate is rejected, the demo shows the no-activation decision while execution paths remain covered by fakes, dry-run tests, and paper-endpoint safety tests.
- Order intent, Alpaca response, fill updates, and reconciliation are auditable.
- The private FastAPI layer can submit bounded research jobs and read reports but cannot submit an order or decrypt either Alpaca credential.
- The dashboard shows system health, candidate evidence, positions, orders, and failures without exposing secrets.
- Docker Compose runs the system locally.
- GitHub Actions pass without external trading calls or stored AWS keys.
- No real-money endpoint, raw broker token, or public raw-data bucket exists.

### Platform showcase — stretch after the two MVP tiers

- Kubernetes manifests run it in k3d/kind and k3s.
- Terraform can create and remove the semester AWS environment.
- The temporary AWS deployment exposes only its HTTPS ingress and has a reviewed cost estimate and budget alerts.
- Cognito and Alpaca OAuth isolate multiple user accounts, or the deployment is explicitly labeled single-user.

## Demonstration flow

A strong final presentation should show one complete story:

1. Start the private collector.
2. Show historical backfill and real-time bar ingestion.
3. Freeze a versioned dataset.
4. Launch a research job from the CLI or dashboard.
5. Show several bounded candidate specifications.
6. Open a failed backtest and its structured revision feedback.
7. Open the final candidate's walk-forward, holdout, bootstrap, cost-stress, and predeclared benchmark comparisons.
8. If it passed, promote that exact immutable artifact to shadow mode; if it failed, show `REJECTED`, prove that activation is blocked, and skip steps 9–12.
9. Show a fresh LONG, SHORT, or FLAT decision with calibrated probability.
10. Show the independent risk decision.
11. With explicit paper authorization, submit or display a paper order.
12. Show the Alpaca paper fill, reconciliation, and tamper-evident audit trail.
13. Demonstrate the kill switch or a stale-data rejection.
14. Show Docker Compose, then local Kubernetes and the temporary AWS deployment if the platform showcase was completed.
15. End with limitations and cost disclosure.

The strongest claim is not "the AI makes money." It is:

> Within one fixed semiconductor and network-infrastructure universe, the system can generate bounded XGBoost candidate variations, test them without cheating, compare them with predeclared benchmarks, quantify uncertainty, reject weak candidates, and—only when every gate passes—safely operate the exact approved version in a paper-only environment.

## Limitations

- The fixed eight-stock, thematically concentrated, present-day universe creates selection, survivorship, common-factor, and limited cross-sectional-breadth risk.
- SOXX is an imperfect contextual benchmark because several MVP companies are networking or connectivity businesses rather than pure-semiconductor constituents.
- Free Alpaca/IEX data is not the full consolidated U.S. market.
- Provider history, feed access, rate limits, and asset eligibility can change.
- Paper fills can differ materially from real execution.
- Short borrow availability, borrow fees, market impact, and queue position are simplified.
- Backtest transaction costs are estimates.
- A 95% bootstrap result depends on the model and assumptions; it is not a guarantee.
- Trying many candidates increases overfitting risk even with controls.
- A single-node k3s deployment is not highly available.
- Containers share a host kernel and are not absolute security boundaries.
- Cloud credits expire and cloud pricing changes.
- Historical and paper performance do not guarantee future results.

## Educational and safety disclaimer

This repository is educational research software. It is not investment, financial, legal, tax, or accounting advice and does not recommend buying, selling, or holding any security. Historical simulations and paper results can be wrong, incomplete, or misleading and do not guarantee profitability, capital preservation, or fitness for any purpose.

The supported boundary is Alpaca Paper Trading with simulated capital and simulated fills. Real-money execution is structurally out of scope.
