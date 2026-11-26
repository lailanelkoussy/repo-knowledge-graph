package com.acme.secure.web;

import com.acme.secure.model.Account;
import com.acme.secure.repository.AccountRepository;
import jakarta.validation.Valid;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.core.userdetails.UserDetails;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api")
public class AccountController {

    private final AccountRepository accountRepository;

    public AccountController(AccountRepository accountRepository) {
        this.accountRepository = accountRepository;
    }

    /** IDOR candidate: any authenticated user can read any account. */
    @GetMapping("/accounts/{id}")
    public AccountView get(@PathVariable Long id) {
        return AccountView.of(accountRepository.findById(id).orElseThrow());
    }

    /** Same lookup, but bound to the current user. */
    @GetMapping("/accounts/mine/{id}")
    public SafeAccountView mine(@PathVariable Long id, @AuthenticationPrincipal UserDetails user) {
        return SafeAccountView.of(accountRepository.findByIdAndOwner(id, user.getUsername()));
    }

    /** Mass assignment: the JPA entity is bound from the request body, without validation. */
    @PutMapping("/accounts/{id}")
    public Account update(@PathVariable Long id, @RequestBody Account account) {
        return accountRepository.save(account);
    }

    /** Public registration whose body lets the caller choose a role. */
    @PostMapping("/public/register")
    public SafeAccountView register(@Valid @RequestBody RegisterRequest request) {
        return SafeAccountView.of(accountRepository.save(request.toAccount()));
    }

    /** @PreAuthorize without @EnableMethodSecurity, and the admin rule of the filter chain never applies. */
    @PreAuthorize("hasRole('ADMIN')")
    @DeleteMapping("/admin/accounts/{id}")
    public void delete(@PathVariable Long id) {
        accountRepository.deleteById(id);
    }
}
