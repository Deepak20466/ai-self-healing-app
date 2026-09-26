package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestAverageOfValues(t *testing.T) {
	if got := average([]int{2, 4, 6}); got != 4 {
		t.Fatalf("average = %d, want 4", got)
	}
}

func TestEmptyAverageDoesNotCrash(t *testing.T) {
	srv := httptest.NewServer(newMux())
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/stats/average?values=")
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode == http.StatusInternalServerError {
		t.Fatalf("GET /stats/average with no values returned 500")
	}
}
