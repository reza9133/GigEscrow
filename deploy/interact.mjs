// interact.mjs
//
// Command-line client for the two contracts deployed on Studionet:
//   GigEscrow  0xbf9485b10851ceF84292a3016CE7cfaaFAE6b314
//   TalentGate 0xB288510e39ceFe6dD1F3704629e33743C1c09a93
//
// This exists because the `genlayer` CLI's `write` command has no `--value`
// option (confirmed against the CLI reference), so it cannot sign the two
// payable calls -- fund_milestone and dispute_milestone -- that actually
// move GEN. Every other call here could also be done through the CLI; this
// script exists specifically so those two are not stuck.
//
// Setup:
//   npm install genlayer-js
//   export PRIVATE_KEY=0x...        # the funded wallet you deployed with
//
// Usage:
//   node interact.mjs <command> [...args]
//   node interact.mjs               # lists commands
//
// Every read-only command needs no PRIVATE_KEY. Every write command does --
// without one, a fresh random (unfunded) account is used and write calls
// will fail for lack of gas/value, which is intentional: better an obvious
// failure than silently acting as the wrong account.

import { createClient, createAccount } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { TransactionStatus, ExecutionResult } from "genlayer-js/types";

const GIGESCROW_ADDRESS = "0xbf9485b10851ceF84292a3016CE7cfaaFAE6b314";
const TALENTGATE_ADDRESS = "0xB288510e39ceFe6dD1F3704629e33743C1c09a93";

const account = createAccount(process.env.PRIVATE_KEY);
const client = createClient({ chain: studionet, account });

// ---------------------------------------------------------------------
// Decimal-string GEN amounts -> wei, without floating point, so amounts
// like "0.001" round-trip exactly instead of drifting through Number().
// ---------------------------------------------------------------------
function genToWei(genAmountStr) {
	const s = String(genAmountStr).trim();
	const negative = s.startsWith("-");
	const unsigned = negative ? s.slice(1) : s;
	const [wholePart, fracPart = ""] = unsigned.split(".");
	if (fracPart.length > 18) {
		throw new Error("at most 18 decimal places are supported: " + s);
	}
	const fracPadded = (fracPart + "0".repeat(18)).slice(0, 18);
	const whole = BigInt(wholePart === "" ? "0" : wholePart);
	const frac = BigInt(fracPadded === "" ? "0" : fracPadded);
	const wei = whole * 10n ** 18n + frac;
	return negative ? -wei : wei;
}

// ---------------------------------------------------------------------
// read() / write() wrap the two SDK calls used throughout. write() never
// tries to pull a return value out of the receipt -- a transaction's own
// `data` field holds the *input* (function_name / function_args), not
// whatever the contract method returned -- so every write is followed by
// an explicit view call in the command implementations below, exactly the
// pattern the SDK's own docs use.
// ---------------------------------------------------------------------
async function read(address, functionName, args = []) {
	return client.readContract({ address, functionName, args });
}

async function write(address, functionName, args = [], value = 0n) {
	const hash = await client.writeContract({ address, functionName, args, value });
	const receipt = await client.waitForTransactionReceipt({
		hash,
		status: TransactionStatus.ACCEPTED,
	});
	const succeeded = receipt.txExecutionResultName === ExecutionResult.FINISHED_WITH_RETURN;
	console.log("tx hash:      ", hash);
	console.log("status:       ", receipt.statusName);
	console.log("execution:    ", receipt.txExecutionResultName, succeeded ? "(ok)" : "(FAILED)");
	if (!succeeded) {
		console.log("full receipt: ", JSON.stringify(receipt, null, 2));
	}
	return { hash, receipt, succeeded };
}

function printJson(label, value) {
	console.log(label + ":");
	try {
		console.log(JSON.stringify(JSON.parse(value), null, 2));
	} catch {
		console.log(value); // not JSON (e.g. a plain bool from a view method)
	}
}

// ---------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------

const commands = {
	async "config"() {
		printJson("GigEscrow config", await read(GIGESCROW_ADDRESS, "get_config"));
	},

	async "stats"() {
		printJson("GigEscrow platform stats", await read(GIGESCROW_ADDRESS, "get_platform_stats"));
	},

	async "tiers"() {
		printJson("TalentGate tier thresholds", await read(TALENTGATE_ADDRESS, "get_tier_thresholds"));
	},

	async "job"(jobId) {
		printJson("job " + jobId, await read(GIGESCROW_ADDRESS, "get_job", [Number(jobId)]));
	},

	async "job-milestones"(jobId) {
		printJson("milestones for job " + jobId,
			await read(GIGESCROW_ADDRESS, "get_job_milestones", [Number(jobId)]));
	},

	async "milestone"(jobId, index) {
		printJson("job " + jobId + " milestone " + index,
			await read(GIGESCROW_ADDRESS, "get_milestone", [Number(jobId), Number(index)]));
	},

	async "reputation"(address) {
		printJson("reputation of " + address, await read(GIGESCROW_ADDRESS, "get_reputation", [address]));
	},

	async "eligible"(address, role) {
		console.log(address, "eligible for", role, "->",
			await read(TALENTGATE_ADDRESS, "is_eligible", [address, role]));
	},

	async "create-job"(freelancer, title) {
		if (!freelancer || !title) throw new Error("usage: create-job <freelancer_address> <title>");
		await write(GIGESCROW_ADDRESS, "create_job", [freelancer, title]);
		console.log("\nlook up the new job_id with:");
		console.log("  node interact.mjs jobs-by-client " + account.address);
	},

	async "jobs-by-client"(address) {
		printJson("jobs where " + address + " is the client",
			await read(GIGESCROW_ADDRESS, "get_jobs_by_client", [address]));
	},

	async "jobs-by-freelancer"(address) {
		printJson("jobs where " + address + " is the freelancer",
			await read(GIGESCROW_ADDRESS, "get_jobs_by_freelancer", [address]));
	},

	async "accept-job"(jobId) {
		await write(GIGESCROW_ADDRESS, "accept_job", [Number(jobId)]);
		await commands["job"](jobId);
	},

	async "fund-milestone"(jobId, description, genAmount) {
		if (!jobId || !description || !genAmount) {
			throw new Error("usage: fund-milestone <job_id> <description> <gen_amount>");
		}
		const value = genToWei(genAmount);
		await write(GIGESCROW_ADDRESS, "fund_milestone", [Number(jobId), description], value);
		await commands["job-milestones"](jobId);
	},

	async "submit-milestone"(jobId, index, url, note) {
		if (!jobId || index === undefined || !url) {
			throw new Error("usage: submit-milestone <job_id> <index> <deliverable_url> [note]");
		}
		await write(GIGESCROW_ADDRESS, "submit_milestone",
			[Number(jobId), Number(index), url, note || ""]);
		await commands["milestone"](jobId, index);
	},

	async "approve-milestone"(jobId, index) {
		await write(GIGESCROW_ADDRESS, "approve_milestone", [Number(jobId), Number(index)]);
		await commands["milestone"](jobId, index);
	},

	async "reject-milestone"(jobId, index, reason) {
		await write(GIGESCROW_ADDRESS, "reject_milestone",
			[Number(jobId), Number(index), reason || ""]);
		await commands["milestone"](jobId, index);
	},

	async "dispute-milestone"(jobId, index, bondGenAmount = "0.01") {
		const value = genToWei(bondGenAmount);
		await write(GIGESCROW_ADDRESS, "dispute_milestone", [Number(jobId), Number(index)], value);
		await commands["milestone"](jobId, index);
	},

	async "preview-dispute"(jobId, index) {
		await write(GIGESCROW_ADDRESS, "preview_dispute", [Number(jobId), Number(index)]);
	},

	async "cancel-milestone"(jobId, index) {
		await write(GIGESCROW_ADDRESS, "cancel_milestone", [Number(jobId), Number(index)]);
		await commands["milestone"](jobId, index);
	},

	async "reclaim-milestone"(jobId, index) {
		await write(GIGESCROW_ADDRESS, "reclaim_milestone", [Number(jobId), Number(index)]);
		await commands["milestone"](jobId, index);
	},

	async "request-assignment"(freelancer, role) {
		await write(TALENTGATE_ADDRESS, "request_assignment", [freelancer, role]);
	},

	async "preview-eligibility"(freelancer, role) {
		await write(TALENTGATE_ADDRESS, "preview_eligibility", [freelancer, role]);
	},

	async "whoami"() {
		console.log("signing account:", account.address);
	},
};

// ---------------------------------------------------------------------

const [, , cmd, ...args] = process.argv;

if (!cmd || !(cmd in commands)) {
	console.log("Usage: node interact.mjs <command> [...args]\n");
	console.log("Read-only:");
	console.log("  config | stats | tiers | whoami");
	console.log("  job <job_id> | job-milestones <job_id> | milestone <job_id> <index>");
	console.log("  jobs-by-client <address> | jobs-by-freelancer <address>");
	console.log("  reputation <address> | eligible <address> <role>");
	console.log("\nWrite (needs PRIVATE_KEY):");
	console.log("  create-job <freelancer> <title>");
	console.log("  accept-job <job_id>");
	console.log("  fund-milestone <job_id> <description> <gen_amount>   (payable)");
	console.log("  submit-milestone <job_id> <index> <url> [note]");
	console.log("  approve-milestone <job_id> <index>");
	console.log("  reject-milestone <job_id> <index> [reason]");
	console.log("  dispute-milestone <job_id> <index> [bond_gen_amount=0.01]   (payable)");
	console.log("  preview-dispute <job_id> <index>");
	console.log("  cancel-milestone <job_id> <index>");
	console.log("  reclaim-milestone <job_id> <index>");
	console.log("  request-assignment <freelancer> <role>");
	console.log("  preview-eligibility <freelancer> <role>");
	process.exit(cmd ? 1 : 0);
}

commands[cmd](...args).catch((err) => {
	console.error("\nfailed:", err.message || err);
	process.exit(1);
});
