package com.acme.library.graphql;

import com.acme.library.domain.Book;
import com.acme.library.service.BookService;
import java.util.List;
import org.springframework.graphql.data.method.annotation.QueryMapping;
import org.springframework.stereotype.Controller;

/** Spring for GraphQL resolver (the field name defaults to the method name). */
@Controller
public class BookGraphController {

    private final BookService bookService;

    public BookGraphController(BookService bookService) {
        this.bookService = bookService;
    }

    @QueryMapping
    public List<Book> books() {
        return bookService.all();
    }
}
