
## 4. Data Preprocessing & Feature Engineering
The CIC-IDS-2017 dataset contains over 78 extracted network flow features, presenting a significant risk of the "Curse of Dimensionality" which can severely bottleneck streaming applications. To resolve this, the system employs a **Hybrid Feature Engineering Pipeline**.

**The Spark ML Pipeline consists of the following stages:**
1. **Targeted Feature Extraction:** Critical behavioral indicators (e.g., `Destination Port`, `Flow Duration`, `SYN Flag Count`, `ACK Flag Count`) bypass mathematical reduction to preserve strict contextual rules.
2. **Dimensionality Reduction (PCA):** The remaining 60+ numerical features (packet lengths, inter-arrival times) are routed through a `VectorAssembler` and normalized via a `StandardScaler`. A Principal Component Analysis (PCA) algorithm reduces these dimensions down to $k=10$ (we need to use dynamically computed k) principal components.
3. **Feature Merging:** A final `VectorAssembler` concatenates the hand-picked features with the PCA vectors into a single feature space for the classifier.

## Data Required

1. The Identifiers & Basics

Destination Port: Crucial. If traffic hits Port 80, it's web. If it hits Port 22, it's SSH (often targeted for brute force).

Flow Duration: How long the connection stayed open.

2. Volume & Size Metrics

Total Fwd Packets / Total Backward Packets: How many chunks of data were sent vs. received.

Fwd Packet Length Max/Min/Mean: The size of the data chunks.

Detection Value: If a user sends 5 packets to a server but receives 5,000 massive packets back (Bwd Packet Length Max), your model might flag this as Data Exfiltration or a massive file download.

3. Timing Metrics (The "IAT" Family)

Flow IAT Mean / Max / Min (Inter-Arrival Time): This is the time delay between packets.

Detection Value: Humans browse the web sporadically (high, varied IAT). Automated botnets or brute-force scripts send packets at exact, machine-gun intervals (very low, uniform IAT).

4. TCP Flags (The "Conversation" Rules)

SYN Flag Count, ACK Flag Count, FIN Flag Count: These are the control signals of the network.

Detection Value: As we discussed earlier, a massive spike in SYN Flag Count with zero ACK Flag Count is the exact definition of a SYN Flood DDoS attack.