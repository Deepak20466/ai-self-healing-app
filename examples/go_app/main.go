package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
)

func newMux() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, `{"status":"ok"}`)
	})
	mux.HandleFunc("/stats/average", func(w http.ResponseWriter, r *http.Request) {
		var values []int
		for _, part := range strings.Split(r.URL.Query().Get("values"), ",") {
			if n, err := strconv.Atoi(part); err == nil {
				values = append(values, n)
			}
		}
		fmt.Fprintf(w, `{"average":%d}`, average(values))
	})
	return recoverMiddleware(mux)
}

// recoverMiddleware turns a handler panic into a 500 and an OTel exception.
func recoverMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				recordPanic(r.Context(), rec)
				http.Error(w, "internal error", http.StatusInternalServerError)
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func main() {
	shutdown, err := initTelemetry(context.Background())
	if err != nil {
		log.Fatal(err)
	}
	defer shutdown(context.Background()) //nolint:errcheck

	port := os.Getenv("PORT")
	if port == "" {
		port = "8102"
	}
	log.Printf("go_app listening on %s", port)
	log.Fatal(http.ListenAndServe(":"+port, newMux()))
}
