#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>

#define SERVER_COUNT 3

const char *server_ip = "127.0.0.1";
int ports[SERVER_COUNT] = {5001, 5002, 5003};

int connect_server(int port) {
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0)
        return -1;

    struct timeval timeout = {2, 0};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, server_ip, &addr.sin_addr);

    if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(sock);
        return -1;
    }

    return sock;
}

double probe_server(int port) {
    int sock = connect_server(port);
    if (sock < 0)
        return -1.0;

    struct timespec start, end;
    char reply[128] = {0};

    clock_gettime(CLOCK_MONOTONIC, &start);
    if (send(sock, "PING\n", 5, 0) < 0) {
        close(sock);
        return -1.0;
    }

    ssize_t n = recv(sock, reply, sizeof(reply) - 1, 0);
    clock_gettime(CLOCK_MONOTONIC, &end);
    close(sock);

    if (n <= 0 || strncmp(reply, "PONG", 4) != 0)
        return -1.0;

    return (end.tv_sec - start.tv_sec) * 1000.0 +
           (end.tv_nsec - start.tv_nsec) / 1000000.0;
}

int main(void) {
    double best_time = 1e30;
    int best_index = -1;

    printf("\n=======================================\n");
    printf("     SMART CONTENT DELIVERY SYSTEM     \n");
    printf("=======================================\n");
    printf("Controller: Checking server availability...\n\n");

    for (int i = 0; i < SERVER_COUNT; i++) {
        double delay = probe_server(ports[i]);
        if (delay < 0) {
            printf("Server %d (port %d): OFFLINE\n", i + 1, ports[i]);
        } else {
            printf("Server %d (port %d): ONLINE | Response time: %.2f ms\n",
                   i + 1, ports[i], delay);
            if (delay < best_time) {
                best_time = delay;
                best_index = i;
            }
        }
    }

    if (best_index == -1) {
        printf("\nNo servers available. Request failed.\n");
        return 1;
    }

    int selected_port = ports[best_index];
    printf("\nController: Selecting Server %d\n", best_index + 1);
    printf("Controller: Forwarding request to port %d\n", selected_port);

    int sock = connect_server(selected_port);
    if (sock < 0) {
        printf("Selected server became unavailable.\n");
        printf("Run the client again to select another server.\n");
        return 1;
    }

    const char *request = "GET content.txt\n";
    if (send(sock, request, strlen(request), 0) < 0) {
        perror("Send");
        close(sock);
        return 1;
    }

    printf("\n---------- DELIVERED CONTENT ----------\n");
    char buffer[1024];
    ssize_t n;
    while ((n = recv(sock, buffer, sizeof(buffer), 0)) > 0) {
        fwrite(buffer, 1, n, stdout);
    }
    if (n < 0) {
        perror("Receive");
    }
    printf("\n---------------------------------------\n");
    printf("Content delivered successfully.\n");

    close(sock);
    return 0;
}
