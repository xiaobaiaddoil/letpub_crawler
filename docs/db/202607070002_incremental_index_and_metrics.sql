create table if not exists category_index_state (
    id serial primary key,
    category_id integer not null unique references categories(id),
    field_tag varchar(50) not null unique,
    remote_total_count integer default 0,
    local_index_count integer default 0,
    total_pages integer default 0,
    last_page_count integer default 0,
    estimated_new_count integer default 0,
    status varchar(50) default 'unknown',
    last_checked_at timestamp default now(),
    last_full_scanned_at timestamp
);

create table if not exists category_journal_index (
    id serial primary key,
    category_id integer not null references categories(id),
    journal_id integer not null references journals(journal_id),
    page_no integer,
    position_no integer,
    active boolean default true,
    first_seen_at timestamp default now(),
    last_seen_at timestamp default now(),
    constraint uq_category_journal_index unique (category_id, journal_id)
);

create table if not exists category_page_index (
    id serial primary key,
    category_id integer not null references categories(id),
    page_no integer not null,
    item_count integer default 0,
    first_journal_id integer,
    last_journal_id integer,
    journal_ids_hash varchar(64),
    updated_at timestamp default now(),
    constraint uq_category_page_index unique (category_id, page_no)
);

create table if not exists index_scan_runs (
    id serial primary key,
    mode varchar(50) default 'index_check',
    status varchar(50) default 'running',
    categories_checked integer default 0,
    pages_scheduled integer default 0,
    pages_scanned integer default 0,
    new_journals integer default 0,
    error_message text,
    started_at timestamp default now(),
    finished_at timestamp
);

create table if not exists journal_metric_snapshots (
    id serial primary key,
    journal_id integer not null references journals(journal_id),
    task_id integer references crawl_tasks(id),
    source varchar(50) default 'detail',
    metrics jsonb,
    metric_hash varchar(64),
    crawled_at timestamp default now()
);

create table if not exists journal_metric_changes (
    id serial primary key,
    journal_id integer not null references journals(journal_id),
    task_id integer references crawl_tasks(id),
    source varchar(50) default 'detail',
    field_name varchar(100) not null,
    old_value text,
    new_value text,
    changed_at timestamp default now()
);

create index if not exists idx_category_index_state_status on category_index_state(status);
create index if not exists idx_category_journal_index_journal on category_journal_index(journal_id);
create index if not exists idx_category_journal_index_active on category_journal_index(active);
create index if not exists idx_category_page_index_hash on category_page_index(journal_ids_hash);
create index if not exists idx_journal_metric_snapshots_journal_time on journal_metric_snapshots(journal_id, crawled_at desc);
create index if not exists idx_journal_metric_snapshots_hash on journal_metric_snapshots(metric_hash);
create index if not exists idx_journal_metric_changes_journal_time on journal_metric_changes(journal_id, changed_at desc);
create index if not exists idx_journal_metric_changes_field on journal_metric_changes(field_name);
