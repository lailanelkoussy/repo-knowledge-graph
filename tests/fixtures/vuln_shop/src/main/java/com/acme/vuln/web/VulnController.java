package com.acme.vuln.web;

import com.acme.vuln.service.UserService;
import jakarta.servlet.http.HttpServletRequest;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.expression.spel.standard.SpelExpressionParser;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.client.RestTemplate;

/** Deliberately vulnerable endpoints, each next to its safe variant (test fixture). */
@RestController
@RequestMapping("/api")
public class VulnController {

    private static final Logger log = LoggerFactory.getLogger(VulnController.class);

    private final JdbcTemplate jdbc;
    private final RestTemplate restTemplate;
    private final UserService userService;

    public VulnController(JdbcTemplate jdbc, RestTemplate restTemplate, UserService userService) {
        this.jdbc = jdbc;
        this.restTemplate = restTemplate;
        this.userService = userService;
    }

    @GetMapping("/users")
    public List<Map<String, Object>> search(@RequestParam String name) {
        String sql = "SELECT * FROM users WHERE name = '" + name + "'";
        return jdbc.queryForList(sql);
    }

    @GetMapping("/users/safe")
    public List<Map<String, Object>> searchSafe(@RequestParam String name) {
        return jdbc.queryForList("SELECT * FROM users WHERE name = ?", name);
    }

    @GetMapping("/users/{id}")
    public Map<String, Object> byId(@PathVariable String id) {
        long userId = Long.parseLong(id);
        return jdbc.queryForMap("SELECT * FROM users WHERE id = " + userId);
    }

    @GetMapping("/users/by-email")
    public Object byEmail(HttpServletRequest request) {
        String email = request.getParameter("email");
        return jdbc.queryForList("SELECT * FROM users WHERE email = '" + email + "'");
    }

    @GetMapping("/users/find")
    public Object find(@RequestParam String name) {
        return userService.findByName(name);
    }

    @GetMapping("/ping")
    public String ping(@RequestParam String host) throws IOException {
        Runtime.getRuntime().exec("ping -c 1 " + host);
        return "ok";
    }

    @GetMapping("/ping/safe")
    public String pingSafe() throws IOException {
        new ProcessBuilder("ping", "-c", "1", "localhost").start();
        return "ok";
    }

    @GetMapping("/files")
    public byte[] download(@RequestParam String file) throws IOException {
        return Files.readAllBytes(Paths.get("/data/" + file));
    }

    @GetMapping("/fetch")
    public String fetch(@RequestParam String url) {
        return restTemplate.getForObject(url, String.class);
    }

    @GetMapping("/fetch/safe")
    public String fetchSafe() {
        return restTemplate.getForObject("https://status.example.com/health", String.class);
    }

    @PostMapping("/eval")
    public Object eval(@RequestBody String expression) {
        return new SpelExpressionParser().parseExpression(expression).getValue();
    }

    @GetMapping("/audit")
    public String audit(@RequestParam String user) {
        log.info("audit requested for " + user);
        log.info("audit requested for {}", user.replace("\n", ""));
        return "ok";
    }
}
