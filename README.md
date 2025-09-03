<div align="center">
  <h1>TerraFlow Genomics</h1>
  <p>
    <strong>An automated, cloud-based platform designed to accelerate the discovery of life-changing genetic insights.</strong>
  </p>
  
  <p>
    <a href="#"><img src="https://img.shields.io/badge/Project-Complete-green?style=for-the-badge" alt="Project Status"></a>
  </p>

  <p>
    <a href="#"><img src="https://img.shields.io/badge/Terraform-7B42BC?style=for-the-badge&logo=terraform&logoColor=white" alt="Terraform"></a>
    <a href="#"><img src="https://img.shields.io/badge/AWS-232F3E?style=for-the-badge&logo=amazon-aws&logoColor=white" alt="AWS"></a>
    <a href="#"><img src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker"></a>
    <a href="#"><img src="https://img.shields.io/badge/Apache%2C%20Airflow-017CEE?style=for-the-badge&logo=apacheairflow&logoColor=white" alt="Apache Airflow"></a>
  </p>
</div>

<!-- This is your banner image. It will be centered automatically. -->
<div align="center">
  <img src="docs/assets/Genomics England Banner Long.jpg" alt="Abstract DNA sequencing visualization" width="70%"/>
</div>

---

## 🧭 The Odyssey: Why This Project Exists

This project was born from a profound, life-changing journey through the world of genomic medicine. It is a tribute to the pioneering work of NHS England and its **100,000 Genomes Project**.

> The NHS 100,000 Genomes Project is a landmark UK initiative aimed at sequencing the complete genetic codes of patients with rare diseases and their families. By creating this massive dataset, it seeks to uncover new diagnoses and pave the way for personalized medicine.

For eighteen years, my life was an odyssey through a fog of medical uncertainty with no linking diagnosis. As part of the project, my mother, my father, and I all donated our blood. Our entire genomes would be sequenced and explored, becoming three more data points in a vast ocean of information. We were told it was unlikely we would hear anything back.

<table align="center" border="0" cellspacing="0" cellpadding="10">
  <tr>
    <td align="center">
      <img src="docs/assets/Family_Samples.jpg" alt="DNA samples from my family for the project" width="300"/>
    </td>
    <td align="center">
      <img src="docs/assets/family_samples_2.jpg" alt="A closer view of the genomic samples" width="300"/>
    </td>
  </tr>
</table>

A year later, against all odds, I received a result. The project had delivered a link. In the three billion letters of my genetic code, they had found a single, ultra-rare mutation in the **COA3 gene**, causing a mitochondrial defect.

<table align="center" border="0" cellspacing="0" cellpadding="10">
  <tr>
    <td align="center">
      <img src="docs/assets/Mutation_highlight.jpg" alt="A DNA sequence with one letter highlighted in red to show a mutation" width="300"/>
    </td>
    <td align="center">
      <img src="docs/assets/Genome_Family_Tree.jpg" alt="A family tree showing the inheritance of the genetic mutation" width="300"/>
    </td>
  </tr>
</table>

The statistical probability of this discovery is staggering. The odds of both parents carrying the same rare recessive fault and passing it on is approximately **1 in 64 million**.

### The 1 in 64 Million Chance

This isn't a random number; it's grounded in a core concept of population genetics known as the **Hardy-Weinberg principle**. It's the scientific method for estimating how rare a genetic trait is. The principle is expressed with this elegant equation:

$$ p^2 + 2pq + q^2 = 1 $$

This describes how genetic variations are distributed in a population, where `2pq` represents the probability of being a carrier—like my parents. The calculation itself is a straightforward product of these rare probabilities:

*   **🧑 Carrier Frequency:** The estimated chance of one person being a carrier (`2pq`) is **1 in 8,000**.
*   **🧑‍🤝‍🧑 Two Carriers Meet:** The probability of two such carriers meeting is therefore:

<p align="center">
  <code>(1 / 8,000)   ×   (1 / 8,000)   =   1 / 64,000,000</code>
</p>

My donated genome will now serve as a data point to help others, ensuring that a single-letter fault doesn't define another patient's life. My odyssey of discovery took two decades, but it doesn't need to be that way for others. With technologies like TerraFlow Genomics, we can accelerate these discoveries and build solutions that may one day repair the very code of life itself.

<table align="center" border="0" cellspacing="0" cellpadding="10">
  <tr>
    <td align="center" width="32%">
      <img src="docs/assets/helping_hands.jpg" alt="Hands reaching out to form a double helix, representing helping others."/>
      <br><em>We can help eachother</em>
    </td>
    <td align="center" width="32%">
      <img src="docs/assets/odyssey.jpg" alt="A person climbing a DNA strand, representing the long diagnostic journey."/>
      <br><em>By looking within ourselves</em>
    </td>
    <td align="center" width="32%">
      <img src="docs/assets/future_solutions.jpg" alt="DNA being knitted or repaired, representing future solutions."/>
      <br><em>To build a better future</em>
    </td>
  </tr>
</table>

---

## 💡 What is TerraFlow Genomics?

Think of TerraFlow Genomics as an **automated, robotic science lab in the cloud.** It takes the complex, manual, and expensive process of genome analysis and transforms it into an efficient, scalable assembly line.

*   ⚙️ **Fully Automated:** Scientists can launch complex analyses with a single click, eliminating manual setup and human error.
*   🚀 **Infinitely Scalable:** The platform can summon the power of hundreds of computers to process massive datasets in parallel, then vanish when the work is done.
*   💰 **Cost-Effective:** By using cloud resources on-demand, we only pay for the exact compute time used, drastically reducing the cost compared to owning physical servers.
*   🔬 **Scientifically Rigorous:** Every step is standardized and reproducible, ensuring the results are reliable and trustworthy.

## 🤔 The Challenge: From a DNA Sample to an Answer

Analyzing genomes is incredibly difficult. Scientists face three major hurdles:

*   🌊 **The Data Deluge:** A single human genome can be over 100 gigabytes. Analyzing hundreds requires a staggering amount of storage and power.
*   🧩 **The Complex Recipe:** The analysis involves dozens of specialized scientific tools, each needing to be run in a specific order. One mistake can invalidate the results.
*   🚧 **The Hardware Hurdle:** This work traditionally requi
