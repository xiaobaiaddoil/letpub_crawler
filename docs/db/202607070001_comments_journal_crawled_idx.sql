CREATE INDEX IF NOT EXISTS idx_comments_journal_crawled_at
    ON comments (journal_id, crawled_at DESC);
