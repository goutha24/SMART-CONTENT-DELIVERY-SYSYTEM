# Smart Content Delivery System Using Software-Defined Networking (SDN)

**CN Mini-Project (Jackfruit Problem)**  
**Team Members:**
- Gururaj M Omkar - PES2UG25AM099
- Goutham Shanthkumar - PES2UG25AM096
- Neil Adoni - PES2UG25AM066

## Project Structure
- `server.c` — TCP content server supporting `PING` health/latency probes, `GET` content requests, and configurable artificial response delays.
- `client.c` — Client & controller probe logic that pings servers on ports `5001`, `5002`, and `5003`, selects the online server with the lowest response time, and fetches `content.txt`.
- `content/content.txt` — Sample content file directory matching the project setup.
- `Makefile` — Build script to compile `server` and `client`.

## Compilation & Execution
1. Compile the server and client:
   ```bash
   make
   # Or manually:
   gcc server.c -o server
   gcc client.c -o client
   ```

2. Start three server instances in separate terminals (matching the test configuration):
   - **Terminal 1 (Server 1):**
     ```bash
     ./server 5001 "Content from Server 1 (Delay: 300ms)" 300
     ```
   - **Terminal 2 (Server 2):**
     ```bash
     ./server 5002 "Content from Server 2 (Delay: 100ms)" 100
     ```
   - **Terminal 3 (Server 3):**
     ```bash
     ./server 5003 "Content from Server 3 (Delay: 200ms)" 200
     ```

3. Run the client in a fourth terminal:
   ```bash
   ./client
   ```
