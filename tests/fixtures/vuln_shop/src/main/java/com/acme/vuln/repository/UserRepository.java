package com.acme.vuln.repository;

import java.util.List;
import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class UserRepository {

    private final JdbcTemplate jdbc;

    public UserRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public List<Map<String, Object>> findByNameUnsafe(String name) {
        return jdbc.queryForList("SELECT * FROM users WHERE name = '" + name + "'");
    }

    /** Concatenates its argument, but every caller passes a constant. */
    public List<Map<String, Object>> findByStatus(String status) {
        return jdbc.queryForList("SELECT * FROM users WHERE status = '" + status + "'");
    }

    public List<Map<String, Object>> active() {
        return findByStatus("ACTIVE");
    }

    public Integer countOlderThan(long age) {
        return jdbc.queryForObject("SELECT COUNT(*) FROM users WHERE age > " + age, Integer.class);
    }
}
