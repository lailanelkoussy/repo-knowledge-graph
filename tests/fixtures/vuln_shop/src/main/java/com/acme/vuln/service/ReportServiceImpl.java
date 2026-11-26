package com.acme.vuln.service;

import java.io.IOException;
import org.springframework.stereotype.Service;

@Service
public class ReportServiceImpl implements ReportService {

    @Override
    public String render(String format) {
        try {
            Runtime.getRuntime().exec("report-tool --format " + format);
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
        return "ok";
    }
}
