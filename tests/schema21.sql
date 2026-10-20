-- Historical schema fixture for migration preservation tests; no personal records.
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL UNIQUE CHECK (role IN ('owner', 'auditor')),
    username TEXT NOT NULL,
    usernameKey TEXT NOT NULL UNIQUE,
    passwordHash TEXT NOT NULL,
    authVersion INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE integrationSecrets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL CHECK (length(trim(label)) > 0),
    secret TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    accountClass TEXT NOT NULL CHECK (accountClass IN ('cash', 'credit', 'system')),
    accountSubtype TEXT CHECK (
        (accountClass = 'cash' AND (accountSubtype IS NULL OR accountSubtype IN ('checking', 'savings', 'other')))
        OR (accountClass = 'credit' AND (accountSubtype IS NULL OR accountSubtype IN ('Credit Card', 'Personal Loan', 'Other')))
        OR (accountClass = 'system' AND accountSubtype IS NULL)
    ),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    displayOrder INTEGER NOT NULL DEFAULT 0,
    balanceOffsetCents INTEGER NOT NULL DEFAULT 0 CHECK (typeof(balanceOffsetCents) = 'integer')
    ,creditLimitCents INTEGER CHECK (creditLimitCents IS NULL OR (typeof(creditLimitCents) = 'integer' AND creditLimitCents > 0))
    ,minimumBalanceCents INTEGER CHECK (minimumBalanceCents IS NULL OR (typeof(minimumBalanceCents) = 'integer' AND minimumBalanceCents > 0))
    ,minimumCautionPercent INTEGER CHECK (minimumCautionPercent IS NULL OR (typeof(minimumCautionPercent) = 'integer' AND minimumCautionPercent >= 0))
    ,utilizationWarningBasisPoints INTEGER CHECK (utilizationWarningBasisPoints IS NULL OR (typeof(utilizationWarningBasisPoints) = 'integer' AND utilizationWarningBasisPoints BETWEEN 1 AND 10000))
    ,systemIdentity TEXT UNIQUE CHECK (systemIdentity IS NULL OR systemIdentity = 'external')
    , pinnedForSelection INTEGER NOT NULL DEFAULT 0 CHECK (pinnedForSelection IN (0, 1) AND (pinnedForSelection = 0 OR (active = 1 AND accountClass IN ('cash', 'credit')))),CHECK (accountClass != 'system' OR (systemIdentity = 'external' AND name = 'External' AND active = 1 AND balanceOffsetCents = 0 AND creditLimitCents IS NULL AND minimumBalanceCents IS NULL AND minimumCautionPercent IS NULL AND utilizationWarningBasisPoints IS NULL))
);
CREATE TABLE categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    parentCategoryId INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    displayOrder INTEGER NOT NULL DEFAULT 0,
    offerReimbursement INTEGER NOT NULL DEFAULT 0 CHECK (offerReimbursement IN (0, 1)),
    reportingRole TEXT NOT NULL DEFAULT 'expense' CHECK (reportingRole IN ('expense', 'income')),
    CHECK (parentCategoryId IS NULL OR parentCategoryId != id)
);
CREATE TABLE applicationSettings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE transactionTemplates (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    accountId INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    description TEXT CHECK (description IS NULL OR length(trim(description)) > 0),
    categoryId INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    enteredAmountCents INTEGER CHECK (enteredAmountCents IS NULL OR (typeof(enteredAmountCents) = 'integer' AND enteredAmountCents != 0)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    displayOrder INTEGER NOT NULL DEFAULT 0,
    prefillAmount INTEGER NOT NULL DEFAULT 1 CHECK (prefillAmount IN (0, 1))
);
CREATE TABLE sinkingFundPlans (
    id INTEGER PRIMARY KEY,
    templateId INTEGER NOT NULL REFERENCES transactionTemplates(id) ON DELETE RESTRICT,
    frequency TEXT NOT NULL CHECK (frequency IN ('monthly', 'quarterly', 'yearly')),
    expectedDate TEXT NOT NULL CHECK (
        length(expectedDate) = 10
        AND expectedDate GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND date(expectedDate, '+0 days') = expectedDate
    ),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE TABLE flexibleCategoryBudgets (
    id INTEGER PRIMARY KEY,
    categoryId INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    monthlyTargetCents INTEGER NOT NULL CHECK (typeof(monthlyTargetCents) = 'integer' AND monthlyTargetCents > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE TABLE expectedIncomePlans (
    id INTEGER PRIMARY KEY,
    templateId INTEGER NOT NULL REFERENCES transactionTemplates(id) ON DELETE RESTRICT,
    expectedDate TEXT NOT NULL CHECK (length(expectedDate) = 10 AND date(expectedDate, '+0 days') = expectedDate),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE TABLE plannedCreditPayments (
    id INTEGER PRIMARY KEY,
    expectedDate TEXT NOT NULL CHECK (length(expectedDate) = 10 AND date(expectedDate, '+0 days') = expectedDate),
    accountId INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    amountCents INTEGER NOT NULL CHECK (typeof(amountCents) = 'integer' AND amountCents > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE TABLE simulatorPlans (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    planDate TEXT NOT NULL CHECK (length(planDate) = 10 AND date(planDate, '+0 days') = planDate)
);
CREATE TABLE simulatorRows (
    id INTEGER PRIMARY KEY,
    planId INTEGER NOT NULL REFERENCES simulatorPlans(id) ON DELETE CASCADE,
    rowType TEXT NOT NULL CHECK (rowType IN ('live', 'income', 'spend', 'transfer', 'buffer')),
    rowOrder INTEGER NOT NULL,
    fromAccountId INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    toAccountId INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    categoryId INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    label TEXT,
    amountCents INTEGER CHECK (amountCents IS NULL OR (typeof(amountCents) = 'integer' AND amountCents > 0)),
    amountMode TEXT NOT NULL DEFAULT 'fixed' CHECK (amountMode IN ('fixed', 'payoff')),
    executed INTEGER NOT NULL DEFAULT 0 CHECK (executed IN (0, 1)),
    executedTransactionId INTEGER REFERENCES transactions(id) ON DELETE SET NULL
);
CREATE TABLE pendingSubmissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            integrationId INTEGER REFERENCES integrationSecrets(id) ON DELETE RESTRICT,
            submissionId TEXT NOT NULL,
            originalContent TEXT NOT NULL,
            reviewContent TEXT,
            reviewVersion INTEGER NOT NULL DEFAULT 0 CHECK (reviewVersion >= 0),
            originalRole TEXT CHECK (originalRole IN ('income', 'expense')),
            kind TEXT NOT NULL DEFAULT 'transaction' CHECK (kind IN ('transaction', 'transfer')),
            source TEXT NOT NULL DEFAULT 'integration' CHECK (source IN ('integration', 'manual')),
            pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
            sourceSnapshot TEXT,
            status TEXT NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Approved', 'Rejected')),
            transactionId INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
            transferId TEXT,
            CHECK ((source = 'manual' AND integrationId IS NULL) OR (source = 'integration' AND integrationId IS NOT NULL)),
            CHECK ((kind = 'transaction' AND originalRole IS NOT NULL AND transferId IS NULL) OR (kind = 'transfer' AND originalRole IS NULL AND transactionId IS NULL)),
            UNIQUE(integrationId, submissionId)
        );
CREATE TABLE "transactions" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accountId INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    date TEXT NOT NULL CHECK (
        length(date) = 10
        AND date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND date(date, '+0 days') = date
    ),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    amountCents INTEGER NOT NULL CHECK (typeof(amountCents) = 'integer'),
    categoryId INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    eventIdentity TEXT,
    transferId TEXT,
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1))
);
CREATE UNIQUE INDEX oneActiveSinkingPlanPerTemplate
ON sinkingFundPlans(templateId) WHERE active = 1;
CREATE UNIQUE INDEX oneActiveFlexibleBudgetPerCategory
ON flexibleCategoryBudgets(categoryId) WHERE active = 1;
CREATE INDEX simulatorRowsPlanIndex ON simulatorRows(planId, rowOrder);
CREATE UNIQUE INDEX manualSubmissionIdentity ON pendingSubmissions(submissionId) WHERE source = 'manual';
CREATE INDEX pendingStatusIndex ON pendingSubmissions(status, id);
CREATE TRIGGER ledgerEventIdentity AFTER INSERT ON transactions WHEN NEW.eventIdentity IS NULL
                    BEGIN UPDATE transactions SET eventIdentity = lower(hex(randomblob(16))) WHERE id = NEW.id; END;
CREATE TRIGGER simulatorTransactionUnlink
BEFORE DELETE ON transactions
BEGIN
    UPDATE simulatorRows
    SET executed = 0, executedTransactionId = NULL
    WHERE executedTransactionId = OLD.id;
END;
CREATE INDEX transactionsDateIndex ON transactions(date);
CREATE INDEX transactionsAccountIndex ON transactions(accountId);
CREATE INDEX transactionsCategoryIndex ON transactions(categoryId);
CREATE INDEX transactionsTransferIndex ON transactions(transferId);
PRAGMA user_version = 21;
