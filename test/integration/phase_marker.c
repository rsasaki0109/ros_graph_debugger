/* Test-only LTTng marker RPC. Build: cc phase_marker.c $(pkg-config --cflags --libs lttng-ust) -o phase_marker */
#define _GNU_SOURCE
#include <lttng/tracef.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/time.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <errno.h>

static volatile sig_atomic_t stopped;
static void stop(int sig) { (void)sig; stopped = 1; }
static double now(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec + t.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
  if (argc != 2) return 2;
  struct sockaddr_un address = {.sun_family = AF_UNIX};
  if (strlen(argv[1]) >= sizeof(address.sun_path)) return 2;
  strcpy(address.sun_path, argv[1]);
  signal(SIGINT, stop);
  signal(SIGTERM, stop);
  signal(SIGPIPE, SIG_IGN);
  double deadline = now() + 20;
  /* Readiness is the enabled provider, not a fixed registration delay. */
  while (!stopped && !tracepoint_enabled(lttng_ust_tracef, event)) {
    if (now() >= deadline) { fputs("tracef provider not enabled\n", stderr); return 1; }
    struct timespec delay = {.tv_nsec = 10000000};
    nanosleep(&delay, NULL);
  }
  if (stopped) return 1;
  int listener = socket(AF_UNIX, SOCK_STREAM, 0);
  if (listener < 0) return 1;
  if (bind(listener, (struct sockaddr *)&address, sizeof(address)) || listen(listener, 4)) {
    perror("marker socket"); close(listener); return 1;
  }
  puts("RGD_MARKER_READY"); fflush(stdout);
  int status = 0;
  while (!stopped) {
    struct pollfd descriptor = {.fd = listener, .events = POLLIN};
    int available = poll(&descriptor, 1, 100);
    if (available < 0 && errno == EINTR) continue;
    if (available < 0) { status = 1; break; }
    if (!available) continue;
    int client = accept(listener, NULL, NULL);
    if (client < 0) { if (errno == EINTR) continue; status = 1; break; }
    struct timeval timeout = {.tv_sec = 2};
    setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    char text[2048]; size_t used = 0; int complete = 0;
    while (used < sizeof(text) - 1) {
      ssize_t length = read(client, text + used, 1);
      if (length <= 0) break;
      if (text[used] == '\n') { complete = 1; break; }
      used++;
    }
    text[used] = 0;
    if (!complete || !used || !tracepoint_enabled(lttng_ust_tracef, event)) {
      send(client, "ERROR\n", 6, MSG_NOSIGNAL); close(client); status = 1; break;
    }
    tracef("RGD_PHASE %s", text);
    /* ACK follows trace emission; the coordinator never timestamps the event. */
    if (send(client, "ACK\n", 4, MSG_NOSIGNAL) != 4) status = 1;
    close(client);
    if (status) break;
  }
  close(listener);
  unlink(address.sun_path);
  return status;
}
