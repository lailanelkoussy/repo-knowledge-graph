package com.acme.vuln.web;

import com.acme.vuln.mapper.UserMapper;
import com.acme.vuln.repository.UserRepository;
import com.acme.vuln.service.ReportService;
import com.acme.vuln.service.StorageService;
import org.apache.commons.io.FilenameUtils;
import java.util.List;
import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

/** Inter-procedural flows (S3): each one next to the same code made safe. */
@RestController
@RequestMapping("/flow")
public class FlowController {

    private final JdbcTemplate jdbc;
    private final UserMapper userMapper;
    private final UserRepository userRepository;
    private final ReportService reportService;
    private final StorageService storageService;

    public FlowController(JdbcTemplate jdbc, UserMapper userMapper, UserRepository userRepository,
                          ReportService reportService, StorageService storageService) {
        this.jdbc = jdbc;
        this.userMapper = userMapper;
        this.userRepository = userRepository;
        this.reportService = reportService;
        this.storageService = storageService;
    }

    /** MyBatis ${order} reached from a request parameter. */
    @GetMapping("/users/ordered")
    public List<Map<String, Object>> ordered(@RequestParam String order) {
        return userMapper.listOrdered(order);
    }

    /** The value goes through a helper that returns it unchanged, then into a concatenated query. */
    @GetMapping("/users/normalized")
    public List<Map<String, Object>> normalized(@RequestParam String name) {
        String clean = normalize(name);
        return jdbc.queryForList("SELECT * FROM users WHERE name = '" + clean + "'");
    }

    /** Same helper, but the query binds the value. */
    @GetMapping("/users/normalized/safe")
    public List<Map<String, Object>> normalizedSafe(@RequestParam String name) {
        return jdbc.queryForList("SELECT * FROM users WHERE name = ?", normalize(name));
    }

    /** Through an interface: ReportServiceImpl runs a command built from the argument. */
    @PostMapping("/reports")
    public String report(@RequestParam String format) {
        return reportService.render(format);
    }

    /** Path traversal through a service, and the same call after keeping only the file name. */
    @GetMapping("/files")
    public byte[] file(@RequestParam String name) {
        return storageService.read(name);
    }

    @GetMapping("/files/safe")
    public byte[] fileSafe(@RequestParam String name) {
        return storageService.read(FilenameUtils.getName(name));
    }

    /** A number cannot carry SQL. */
    @GetMapping("/users/count")
    public Object count(@RequestParam long minAge) {
        return userRepository.countOlderThan(minAge);
    }

    private String normalize(String value) {
        return value.trim();
    }
}
